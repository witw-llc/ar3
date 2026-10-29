"""Keeps the tool tests out of the real state directories and out of a real Chrome, and fails the run when one gets in.

The tools keep their seats, sessions and vault under `${XDG_DATA_HOME:-~/.local/share}/ar3/`: the browser
seats (Chrome profiles, locks, secrets), the Gemini sessions and the vault. A machine that runs live nodes
keeps them in the same place. No test may read or write there. The same goes for the a8s home
(`A8S_HOME`, else `~/.config/a8s`), which the tests reach when they run `a8s tell`.

Three layers, all in this one plugin, which `conftest.py` loads with `pytest_plugins`:

- A floor for the whole session. `XDG_DATA_HOME`, `XDG_CACHE_HOME` and `A8S_HOME` point at a temp directory
  from the moment pytest is configured, so collection, session fixtures and every subprocess that inherits
  the environment are covered.
- A directory of its own for each test, in an autouse fixture. A seat's state does not outlive the test.
- A guard. An audit hook (PEP 578) watches the file-system calls this process makes. A call that would
  create or change anything under the real roots is refused and recorded, and the test fails at its teardown
  even if the code under test swallowed the refusal. When the session ends, the top-level names under the
  tool roots are compared with what they were at the start, which catches a subprocess that wrote there.
  (A live node keeps writing inside its own seat directory while the tests run, so only the names at the
  top of a root are compared, never the files inside a seat. A live a8s keeps its home in use in the same
  way, so for the a8s homes the check is only that none appeared where there was none.)

Why the isolation lives in a plugin and not in a `conftest.py` beside the tests: in pytest 9.1, where this
was found, the autouse fixtures of a directory's conftest belong to the one collection node made for that
directory. An argument list that visits a directory, leaves it and visits it again
(`browser/a.py tests/b.py browser/c.py`) makes a second node for it, and the second node has none of the
conftest's fixtures. The tests under it then ran against the real state directory. A plugin's autouse
fixtures belong to the session and reach every test whatever order the arguments come in.

The real roots are read once, when this module is imported, before any isolation changes the environment.
Each tool has two: the place the environment names (`XDG_DATA_HOME`) and the default place on the machine
(`~/.local/share`), which a test reaches when it clears the variable. A run that starts with `XDG_DATA_HOME`
set to a temp directory still guards the machine's own.

The same plugin holds the guard against a real Chrome: a test that is not a live test and reaches the
launch of Chrome fails (`_no_real_chrome`).
"""
import os
import shutil
import sys
import tempfile

import pytest

# The state directories of the tools under apps/tools, below `<data home>/ar3/`.
TOOL_STATE = ("browser", "gemini-web", "vault")
# Opening a file for any of these changes it or makes it.
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
# The audit events that create, change, move or remove a file or a directory, and which of their arguments
# name the thing changed. "open" is judged by its flags.
PATH_ARGS = {
    "os.mkdir": (0,), "os.rmdir": (0,), "os.remove": (0,), "os.chmod": (0,), "os.chown": (0,),
    "os.utime": (0,), "os.truncate": (0,), "os.mkfifo": (0,), "os.mknod": (0,),
    "os.rename": (0, 1), "os.link": (0, 1), "os.symlink": (1,),
    "shutil.copyfile": (1,), "shutil.copymode": (1,), "shutil.copystat": (1,),
    "shutil.copytree": (1,), "shutil.move": (0, 1), "shutil.rmtree": (0,),
}
WATCHED = frozenset(PATH_ARGS) | {"open"}


def real_state_roots():
    """The state directories of the tools, in each place this process can reach them: where the environment as
    it stands names them (`XDG_DATA_HOME`), then the default place (`~/.local/share`). A run with the variable
    unset has the three tool directories once."""
    found = []
    for base in (os.environ.get("XDG_DATA_HOME"), os.path.expanduser("~/.local/share")):
        if base and base not in found:
            found.append(base)
    return tuple(os.path.join(base, "ar3", name) for base in found for name in TOOL_STATE)


def real_a8s_homes():
    """Where `a8s` may keep its registry and logs: `A8S_HOME`, `$XDG_CONFIG_HOME/a8s`, `~/.config/a8s`, and the
    legacy `~/.a8s`."""
    override = os.environ.get("A8S_HOME", "").strip()
    config = os.environ.get("XDG_CONFIG_HOME", "").strip()
    found = []
    for home in (
        os.path.expanduser(override) if override else "",
        os.path.join(os.path.expanduser(config), "a8s") if config else "",
        os.path.expanduser("~/.config/a8s"),
        os.path.expanduser("~/.a8s"),
    ):
        if home and home not in found:
            found.append(home)
    return tuple(found)


class RealStateTouched(AssertionError):
    """A test reached a real state directory."""


class StateGuard:
    """`roots` are refused to every file-system change this process makes. `listed` (default: `roots`) are the
    ones whose top-level names are compared when the session ends. `appearing` are the ones that must not
    exist at the end if they did not exist at the start: a directory that a live node keeps in use is not
    compared name by name, but a run that makes it from nothing has reached it."""

    def __init__(self, roots, listed=None, appearing=()):
        forms = []
        for root in roots:
            for form in (os.path.abspath(root), os.path.realpath(root)):
                if form not in forms:
                    forms.append(form)
        self.roots = tuple(forms)
        self.bases = tuple(dict.fromkeys(os.path.abspath(root) for root in (roots if listed is None else listed)))
        self.appearing = tuple(dict.fromkeys(os.path.abspath(root) for root in appearing))
        self.violations = []
        self.test = ""
        self.before = self.listing()
        self.existed = {root: os.path.lexists(root) for root in self.appearing}

    def inside(self, path):
        """The root `path` is, or lies under; None for anything else, a file descriptor included."""
        if isinstance(path, (bytes, bytearray)):
            path = os.fsdecode(path)
        elif not isinstance(path, str):
            try:
                path = os.fspath(path)
            except TypeError:
                return None
            if isinstance(path, bytes):
                path = os.fsdecode(path)
        if not path:
            return None
        path = os.path.abspath(path)
        for root in self.roots:
            if path == root or path.startswith(root + os.sep):
                return root
        return None

    def audit(self, event, args):
        """The audit hook. Returns at once for the many events that change nothing."""
        if event not in WATCHED:
            return
        try:
            if event == "open":
                if not isinstance(args[2], int) or not args[2] & WRITE_FLAGS:
                    return
                named = (args[0],)
            else:
                named = [args[index] for index in PATH_ARGS[event] if index < len(args)]
            for path in named:
                root = self.inside(path)
                if root is not None:
                    break
            else:
                return
        except Exception:
            return
        shown = os.fsdecode(path) if isinstance(path, (str, bytes, os.PathLike)) else repr(path)
        what = f"{event} {shown}"
        if self.test:
            what += f" (in {self.test})"
        self.violations.append(what)
        raise RealStateTouched(
            f"refused: {what}. A test may not create or change anything under {root}; "
            "its state belongs in a directory of the test's own (XDG_DATA_HOME)"
        )

    def drain(self):
        """The violations recorded since the last call."""
        found, self.violations = self.violations, []
        return found

    def listing(self):
        """{root: sorted names under it, or None when it does not exist}, for the roots as given."""
        found = {}
        for root in self.bases:
            try:
                found[root] = sorted(os.listdir(root))
            except OSError:
                found[root] = None
        return found

    def changes(self):
        """What was created or removed at the top of the listed roots, and which of the `appearing` roots came
        into being, since this guard was made."""
        after = self.listing()
        lines = []
        for root, names in after.items():
            before = self.before[root]
            if before is None and names is None:
                continue
            if before is None:
                lines.append(f"{root} was created, holding {names}")
            elif names is None:
                lines.append(f"{root} was removed")
            else:
                added = sorted(set(names) - set(before))
                gone = sorted(set(before) - set(names))
                if added:
                    lines.append(f"created under {root}: {', '.join(added)}")
                if gone:
                    lines.append(f"removed under {root}: {', '.join(gone)}")
        for root in self.appearing:
            if not self.existed[root] and os.path.lexists(root):
                lines.append(f"{root} was created")
        return lines


GUARD = StateGuard(real_state_roots() + real_a8s_homes(), listed=real_state_roots(), appearing=real_a8s_homes())
if not getattr(sys, "_ar3_tools_state_guard", None):
    sys.addaudithook(GUARD.audit)
    sys._ar3_tools_state_guard = GUARD

# The variables that name where state lives, and the directory of each under a test's own.
FLOOR = (("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("A8S_HOME", "a8s"))
_floor = {}


def pytest_configure(config):
    """The floor: from here to the end of the session, nothing resolves to the real state."""
    directory = tempfile.mkdtemp(prefix="ar3-tools-session-")
    _floor["directory"] = directory
    for name, leaf in FLOOR:
        _floor[name] = os.environ.get(name)
        os.environ[name] = os.path.join(directory, leaf)
    config.add_cleanup(_lift_floor)


def _lift_floor():
    for name, _leaf in FLOOR:
        if _floor.get(name) is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = _floor[name]
    shutil.rmtree(_floor.get("directory", ""), ignore_errors=True)


def pytest_sessionfinish(session, exitstatus):
    """A subprocess is outside the audit hook. What it wrote shows as a new name at the top of a real root, or
    as an a8s home that was not there before."""
    problems = GUARD.drain() + GUARD.changes()
    if not problems:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep("=", "state guard: the real state directories were touched", red=True)
        for line in problems:
            reporter.write_line(f"  {line}")
    if session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


_RUNTIME = []


def _short_runtime_dir():
    """One short private directory for the whole run, removed when the run ends."""
    if not _RUNTIME:
        import atexit
        import shutil
        import tempfile
        path = tempfile.mkdtemp(prefix="ar3rt-", dir="/tmp" if os.name == "posix" else None)
        atexit.register(shutil.rmtree, path, True)
        _RUNTIME.append(path)
    return _RUNTIME[0]


@pytest.fixture(autouse=True)
def _state_in_tmp(request, tmp_path, monkeypatch):
    """Each test keeps its seats, sessions, vault and a8s files in directories of its own, and fails if it
    reached the real ones."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    # The data home above is too long for a socket, so the driver's socket directory falls back to one under
    # the runtime directory (`plc.ipc_dir`): a short one of the run's own, not the session's.
    monkeypatch.setenv("XDG_RUNTIME_DIR", _short_runtime_dir())
    monkeypatch.setenv("A8S_HOME", str(tmp_path / "a8s-home"))
    GUARD.test = request.node.nodeid
    yield
    GUARD.test = ""
    touched = GUARD.drain()
    if touched:
        pytest.fail("this test touched the real state directories:\n  " + "\n  ".join(touched), pytrace=False)


@pytest.fixture(autouse=True)
def _protection_ends_with_the_test():
    """The values a script holds are protected on its seat while it runs (`browser.plc.protecting`), and with no seat
    named, a message about odd output stays closed while any seat is. A test that protects a seat and does not end it
    (`plc.protect` called by hand, a `Run` that hides a value outside a script) would close every test after it."""
    def clear():
        plc = sys.modules.get("browser.plc")
        if plc is not None:
            plc._protected.clear()

    clear()
    yield
    clear()


@pytest.fixture(autouse=True)
def _no_real_chrome(request, monkeypatch):
    """Only a live test may start a real Chrome (AR3_TOOL_BROWSER_LIVE=1, the switch
    test_stop_live.py skips on). Any other test that reaches the Chrome launch fails
    and names itself, so a stub that misses a call site cannot leave a browser running.
    A test that stubs `plc.run` still runs the real `_launch_chrome` to check its config."""
    if os.environ.get("AR3_TOOL_BROWSER_LIVE") == "1":
        yield
        return
    from browser import plc, session

    launch, run = session._launch_chrome, plc.run
    reached = []

    def refuse(seat):
        if plc.run is not run:
            return launch(seat)
        reached.append(
            f"{request.node.nodeid} reached the real Chrome spawn "
            "(browser.session._launch_chrome). Stub browser.session._start_chrome, "
            "or stub browser.plc.run to check the launch config."
        )
        pytest.fail(reached[-1])

    monkeypatch.setattr(session, "_launch_chrome", refuse)
    yield
    # A thread or a handler may swallow the failure above; the test still fails here.
    assert not reached, reached[0]
