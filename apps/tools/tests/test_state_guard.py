"""The state guard: no test reaches the real state directories, and a test that does fails the run.

The first half drives `StateGuard` on temp directories. The second half runs pytest in a subprocess with
`HOME` in a temp directory and `XDG_DATA_HOME`, `XDG_CACHE_HOME` and `A8S_HOME` unset, so that "the real
state directory" is a directory inside that temp home and nothing here can touch the machine's own.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import state_guard
from state_guard import RealStateTouched, StateGuard

TESTS = Path(__file__).resolve().parent
TOOLS = TESTS.parent
LIB = TOOLS.parent.parent / "lib"


@pytest.fixture
def real(tmp_path):
    root = tmp_path / "real" / "ar3" / "browser"
    return StateGuard([str(root)]), root


# --- the guard -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event, args",
    [
        ("os.mkdir", ("{p}", 0o777, None)),
        ("os.rmdir", ("{p}", None)),
        ("os.remove", ("{p}", None)),
        ("os.chmod", ("{p}", 0o600, None, -1)),
        ("os.chown", ("{p}", 1, 1, None, -1)),
        ("os.utime", ("{p}", None, None, None, -1)),
        ("os.truncate", ("{p}", 0)),
        ("os.rename", ("{p}", "/elsewhere/x", None, None)),
        ("os.rename", ("/elsewhere/x", "{p}", None, None)),
        ("os.link", ("/elsewhere/x", "{p}", None, None)),
        ("os.symlink", ("/elsewhere/x", "{p}", None)),
        ("shutil.copyfile", ("/elsewhere/x", "{p}")),
        ("shutil.copymode", ("/elsewhere/x", "{p}")),
        ("shutil.copystat", ("/elsewhere/x", "{p}")),
        ("shutil.copytree", ("/elsewhere/x", "{p}")),
        ("shutil.move", ("/elsewhere/x", "{p}")),
        ("shutil.move", ("{p}", "/elsewhere/x")),
        ("shutil.rmtree", ("{p}", None)),
        ("open", ("{p}", "w", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)),
        ("open", ("{p}", "a", os.O_WRONLY | os.O_APPEND)),
        ("open", ("{p}", "r+", os.O_RDWR)),
        ("open", ("{p}", None, os.O_CREAT | os.O_RDONLY)),
    ],
)
def test_a_change_under_a_real_root_is_refused_and_recorded(real, event, args):
    guard, root = real
    path = str(root / "seat" / "secrets.json")
    guard.test = "test_x.py::test_y"
    with pytest.raises(RealStateTouched, match="refused") as refused:
        guard.audit(event, tuple(path if arg == "{p}" else arg for arg in args))
    assert str(root) in str(refused.value) and event in str(refused.value)
    (recorded,) = guard.drain()
    assert recorded.startswith(f"{event} ") and path in recorded and recorded.endswith("(in test_x.py::test_y)")
    assert guard.drain() == []


@pytest.mark.parametrize(
    "event, args",
    [
        ("open", ("{p}", "r", os.O_RDONLY)),
        ("open", ("{p}", "rb", os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))),
        ("open", (3, "w", os.O_WRONLY)),
        ("os.symlink", ("{p}", "/elsewhere/x", None)),
        ("shutil.copyfile", ("{p}", "/elsewhere/x")),
        ("shutil.copytree", ("{p}", "/elsewhere/x")),
        ("os.truncate", (5, 0)),
        ("os.listdir", ("{p}",)),
        ("os.scandir", ("{p}",)),
        ("socket.connect", ("{p}",)),
        ("exec", ("{p}",)),
        ("os.mkdir", ()),
        ("os.mkdir", (None, 0o777, None)),
        ("os.mkdir", ("", 0o777, None)),
    ],
)
def test_a_read_a_file_descriptor_and_an_event_that_changes_nothing_are_let_through(real, event, args):
    guard, root = real
    path = str(root / "seat" / "secrets.json")
    guard.audit(event, tuple(path if arg == "{p}" else arg for arg in args))
    assert guard.drain() == []


def test_only_the_root_and_what_is_under_it_counts(real, tmp_path):
    guard, root = real
    for outside in (
        str(root) + "-two/x", str(root.parent), str(root.parent / "gemini-web" / "x"),
        str(tmp_path / "xdg" / "ar3" / "browser" / "x"), "/tmp/other", str(root) + "x",
    ):
        guard.audit("os.mkdir", (outside, 0o777, None))
    assert guard.drain() == []


@pytest.mark.parametrize(
    "spelling",
    [
        lambda root: str(root),
        lambda root: str(root) + "/",
        lambda root: str(root / "a" / ".." / "b"),
        lambda root: str(root / ".." / "browser" / "b"),
        lambda root: os.fsencode(str(root / "seat")),
        lambda root: root / "seat",
    ],
)
def test_the_root_itself_and_any_spelling_of_a_path_under_it_counts(real, spelling):
    guard, root = real
    with pytest.raises(RealStateTouched):
        guard.audit("os.mkdir", (spelling(root), 0o777, None))


def test_a_relative_path_is_read_from_where_the_process_stands(real, monkeypatch):
    guard, root = real
    root.mkdir(parents=True)
    monkeypatch.chdir(root.parent)
    with pytest.raises(RealStateTouched):
        guard.audit("os.mkdir", ("browser/seat", 0o777, None))
    monkeypatch.chdir(root)
    with pytest.raises(RealStateTouched):
        guard.audit("os.remove", ("seat/lock", None))
    guard.audit("os.mkdir", ("../elsewhere", 0o777, None))


def test_a_symlinked_root_is_the_root_by_either_name(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    os.symlink(target, link)
    guard = StateGuard([str(link)])
    for spelling in (link / "x", target / "x"):
        with pytest.raises(RealStateTouched):
            guard.audit("os.mkdir", (str(spelling), 0o777, None))


def test_a_refusal_is_recorded_even_when_the_code_under_test_swallows_it(real):
    guard, root = real
    try:
        guard.audit("os.mkdir", (str(root / "x"), 0o777, None))
    except Exception:
        pass
    assert len(guard.drain()) == 1


def test_the_first_root_found_names_the_refusal(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    guard = StateGuard([str(first), str(second)])
    with pytest.raises(RealStateTouched, match=str(second)):
        guard.audit("os.mkdir", (str(second / "x"), 0o777, None))


def test_a_root_that_appears_or_gains_or_loses_a_name_is_reported(tmp_path):
    root = tmp_path / "real" / "ar3" / "browser"
    guard = StateGuard([str(root)])
    assert guard.changes() == []
    root.mkdir(parents=True)
    assert guard.changes() == [f"{root} was created, holding []"]
    (root / "ghost").mkdir()
    assert guard.changes() == [f"{root} was created, holding ['ghost']"]

    standing = tmp_path / "standing"
    (standing / "alpha").mkdir(parents=True)
    (standing / "beta").mkdir()
    guard = StateGuard([str(standing)])
    assert guard.changes() == []
    (standing / "seat").mkdir()
    (standing / "alpha").rmdir()
    assert guard.changes() == [f"created under {standing}: seat", f"removed under {standing}: alpha"]
    standing.joinpath("seat").rmdir()
    standing.joinpath("beta").rmdir()
    assert guard.changes() == [f"removed under {standing}: alpha, beta"]
    standing.rmdir()
    assert guard.changes() == [f"{standing} was removed"]


def test_only_the_names_at_the_top_are_compared_so_a_live_seat_may_go_on_writing(tmp_path):
    root = tmp_path / "root"
    (root / "alpha").mkdir(parents=True)
    guard = StateGuard([str(root)])
    (root / "alpha" / "last-active").write_text("1")
    (root / "alpha" / "chrome").mkdir()
    assert guard.changes() == []


def test_a_root_can_be_watched_for_changes_without_being_listed(tmp_path):
    watched, listed = tmp_path / "a8s", tmp_path / "browser"
    guard = StateGuard([str(watched), str(listed)], listed=[str(listed)])
    watched.mkdir()
    (watched / "registry").write_text("{}")
    assert guard.changes() == []
    with pytest.raises(RealStateTouched):
        guard.audit("os.mkdir", (str(watched / "x"), 0o777, None))
    listed.mkdir()
    assert guard.changes() == [f"{listed} was created, holding []"]


def test_a_home_that_was_not_there_must_not_appear_and_one_that_was_may_change(tmp_path):
    absent, present = tmp_path / "absent", tmp_path / "present"
    present.mkdir()
    guard = StateGuard([str(absent), str(present)], listed=[], appearing=[str(absent), str(present)])
    assert guard.changes() == []
    (present / "registry.json").write_text("{}")
    (present / "mailboxes").mkdir()
    (present / "registry.json").unlink()
    assert guard.changes() == []
    absent.mkdir()
    assert guard.changes() == [f"{absent} was created"]
    present.joinpath("mailboxes").rmdir()
    present.rmdir()
    assert guard.changes() == [f"{absent} was created"]


def test_the_roots_are_the_state_of_each_tool_in_every_place_it_can_be(monkeypatch, tmp_path):
    names = ("browser", "gemini-web", "vault")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    default = tuple(str(tmp_path / "home" / ".local" / "share" / "ar3" / name) for name in names)
    assert state_guard.real_state_roots() == default
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert state_guard.real_state_roots() == tuple(str(tmp_path / "data" / "ar3" / name) for name in names) + default
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home" / ".local" / "share"))
    assert state_guard.real_state_roots() == default


def test_the_a8s_homes_are_every_place_a8s_may_keep_its_state(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("A8S_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    default = (str(home / ".config" / "a8s"), str(home / ".a8s"))
    assert state_guard.real_a8s_homes() == default
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert state_guard.real_a8s_homes() == (str(tmp_path / "config" / "a8s"),) + default
    monkeypatch.setenv("A8S_HOME", str(tmp_path / "mine"))
    assert state_guard.real_a8s_homes() == (str(tmp_path / "mine"), str(tmp_path / "config" / "a8s")) + default
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("A8S_HOME", "~/.a8s")
    assert state_guard.real_a8s_homes() == (str(home / ".a8s"), str(home / ".config" / "a8s"))


def test_the_session_guard_is_installed_and_watches_the_roots_read_before_any_isolation():
    guard = state_guard.GUARD
    assert getattr(sys, "_ar3_tools_state_guard") is guard
    assert all(
        os.path.basename(base) in state_guard.TOOL_STATE and os.path.basename(os.path.dirname(base)) == "ar3"
        for base in guard.bases
    )
    # The default place is watched whatever XDG_DATA_HOME said when the run started, and so are the a8s homes.
    default = [os.path.abspath(os.path.expanduser(f"~/.local/share/ar3/{name}")) for name in state_guard.TOOL_STATE]
    assert [base for base in guard.bases if base in default] == default
    assert guard.inside(os.path.expanduser("~/.config/a8s/registry.json")) is not None
    # Whatever the machine's real roots are, this test's own state is not among them.
    for name in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "A8S_HOME"):
        assert guard.inside(os.path.join(os.environ[name], "x")) is None


# --- every test has state of its own ------------------------------------------------------------------------


def test_every_test_runs_with_state_directories_of_its_own(tmp_path):
    assert os.environ["XDG_DATA_HOME"] == str(tmp_path / "xdg")
    assert os.environ["XDG_CACHE_HOME"] == str(tmp_path / "xdg-cache")
    assert os.environ["A8S_HOME"] == str(tmp_path / "a8s-home")
    assert not (tmp_path / "xdg").exists()


def test_the_floor_under_the_session_is_a_temp_directory_that_is_not_a_real_root():
    floor = state_guard._floor["directory"]
    assert os.path.isdir(floor) and os.path.basename(floor).startswith("ar3-tools-session-")
    assert state_guard.GUARD.inside(floor) is None
    assert [name for name, _leaf in state_guard.FLOOR] == ["XDG_DATA_HOME", "XDG_CACHE_HOME", "A8S_HOME"]


# --- the whole thing, in a pytest of its own under a temp HOME ----------------------------------------------


def nested(tmp_path, tests, args=None, cwd=None, env=None):
    """pytest in a subprocess, with the plugin and a temp HOME. Returns (result, the real browser root)."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / "conftest.py").write_text(
        f"import sys\nsys.path[:0] = {[str(TESTS), str(TOOLS), str(LIB)]!r}\npytest_plugins = ['state_guard']\n"
    )
    for name, source in tests.items():
        (project / name).write_text(source)
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("XDG_", "PYTEST_", "AR3_TOOL")) and key not in ("A8S_HOME", "HOME")
    }
    environment.update(HOME=str(home), AR3_BROWSER_CHROME="/nonexistent/chrome", **(env or {}))
    done = subprocess.run(
        [sys.executable, "-m", "pytest", *(args or ["."]), "-q", "-p", "no:cacheprovider"],
        cwd=cwd or project, env=environment, capture_output=True, text=True, timeout=120,
    )
    return done, home / ".local" / "share" / "ar3" / "browser"


def test_a_clean_run_passes_and_leaves_the_real_root_alone(tmp_path):
    done, root = nested(tmp_path, {"test_clean.py": (
        "from browser import session\n"
        "def test_a_seat_keeps_its_state_in_a_directory_of_the_tests_own(tmp_path):\n"
        "    session.touch_activity('seat')\n"
        "    assert session.last_activity('seat') is not None\n"
        "    assert session.seat_home('seat').startswith(str(tmp_path))\n"
    )})
    assert done.returncode == 0, done.stdout + done.stderr
    assert "state guard" not in done.stdout
    assert not root.exists() and not (tmp_path / "home" / ".local").exists()


def test_a_test_that_loses_its_isolation_is_refused_before_it_writes_and_fails_the_run(tmp_path):
    done, root = nested(tmp_path, {"test_leak.py": (
        "from browser import session\n"
        "def test_a_test_that_unsets_the_isolation(monkeypatch):\n"
        "    monkeypatch.delenv('XDG_DATA_HOME')\n"
        "    session.touch_activity('seat')\n"
    )})
    assert done.returncode != 0
    assert "RealStateTouched" in done.stdout
    assert f"refused: os.mkdir {root}" in done.stdout
    assert "this test touched the real state directories" in done.stdout
    assert not root.exists()


def test_a_refusal_that_the_code_swallows_still_fails_the_test(tmp_path):
    done, root = nested(tmp_path, {"test_swallow.py": (
        "from browser import session\n"
        "def test_a_handler_that_hides_the_refusal(monkeypatch):\n"
        "    monkeypatch.delenv('XDG_DATA_HOME')\n"
        "    try:\n"
        "        session.touch_activity('seat')\n"
        "    except Exception:\n"
        "        pass\n"
    )})
    assert done.returncode != 0
    assert "this test touched the real state directories" in done.stdout
    assert "1 passed" in done.stdout and "1 error" in done.stdout
    assert not (root / "seat").exists()


@pytest.mark.parametrize("tool", ["browser", "gemini-web", "vault"])
def test_every_tool_state_directory_is_guarded(tmp_path, tool):
    done, root = nested(tmp_path, {"test_tool.py": (
        "import os\n"
        "def test_one(monkeypatch, tmp_path):\n"
        "    monkeypatch.delenv('XDG_DATA_HOME')\n"
        f"    os.makedirs(os.path.expanduser('~/.local/share/ar3/{tool}/x'))\n"
    )})
    assert done.returncode != 0 and "RealStateTouched" in done.stdout
    assert not (root.parent / tool / "x").exists()


def test_the_default_place_is_guarded_in_a_run_that_starts_with_a_data_home_set(tmp_path):
    started = tmp_path / "started"
    done, root = nested(tmp_path, {"test_default.py": (
        "from browser import session\n"
        "def test_a_test_that_unsets_the_isolation(monkeypatch):\n"
        "    monkeypatch.delenv('XDG_DATA_HOME')\n"
        "    session.touch_activity('seat')\n"
    )}, env={"XDG_DATA_HOME": str(started)})
    assert done.returncode != 0
    assert f"refused: os.mkdir {root}" in done.stdout
    assert not root.exists() and not started.exists()


def test_the_a8s_home_is_guarded_too(tmp_path):
    done, root = nested(tmp_path, {"test_a8s.py": (
        "import os\n"
        "def test_one(monkeypatch):\n"
        "    monkeypatch.delenv('A8S_HOME')\n"
        "    with open(os.path.expanduser('~/.config/a8s/a8s.json'), 'w') as handle:\n"
        "        handle.write('{}')\n"
    )})
    assert done.returncode != 0 and "RealStateTouched" in done.stdout
    assert not (tmp_path / "home" / ".config" / "a8s" / "a8s.json").exists()


def test_a_child_process_that_writes_to_a_real_root_fails_the_run_at_the_end(tmp_path):
    done, root = nested(tmp_path, {"test_child.py": (
        "import os, subprocess, sys\n"
        "def test_a_subprocess_with_its_own_idea_of_home():\n"
        "    target = os.path.expanduser('~/.local/share/ar3/browser/ghost')\n"
        "    subprocess.run([sys.executable, '-c', 'import os, sys; os.makedirs(sys.argv[1])', target], check=True)\n"
    )})
    assert done.returncode != 0
    assert "1 passed" in done.stdout
    assert "state guard: the real state directories were touched" in done.stdout
    assert f"{root} was created, holding ['ghost']" in done.stdout


def test_a_child_process_that_makes_the_a8s_home_from_nothing_fails_the_run_at_the_end(tmp_path):
    done, _root = nested(tmp_path, {"test_child_a8s.py": (
        "import os, subprocess, sys\n"
        "def test_a_subprocess_with_its_own_idea_of_home():\n"
        "    target = os.path.expanduser('~/.config/a8s')\n"
        "    subprocess.run([sys.executable, '-c', 'import os, sys; os.makedirs(sys.argv[1])', target], check=True)\n"
    )})
    assert done.returncode != 0
    assert "1 passed" in done.stdout
    assert f"{tmp_path / 'home' / '.config' / 'a8s'} was created" in done.stdout


def test_a_child_that_inherits_the_environment_writes_to_the_floor_not_to_the_real_root(tmp_path):
    done, root = nested(tmp_path, {"test_inherit.py": (
        "import os, subprocess, sys\n"
        "def test_a_child_follows_the_isolated_environment(tmp_path):\n"
        "    code = 'import os; os.makedirs(os.path.join(os.environ[\"XDG_DATA_HOME\"], \"seat\"))'\n"
        "    subprocess.run([sys.executable, '-c', code], check=True)\n"
        "    assert (tmp_path / 'xdg' / 'seat').is_dir()\n"
    )})
    assert done.returncode == 0, done.stdout
    assert not root.exists()


def test_the_floor_covers_code_that_runs_outside_every_test(tmp_path):
    done, root = nested(tmp_path, {"test_collect.py": (
        "import os\n"
        "from browser import session\n"
        "session.touch_activity('at-collection')\n"
        "def test_one():\n"
        "    assert os.path.isdir(session.seat_home('at-collection')) is False\n"
    )})
    assert done.returncode == 0, done.stdout
    assert not root.exists()


def test_a_test_that_reaches_the_launch_of_chrome_fails_and_starts_nothing(tmp_path):
    done, root = nested(tmp_path, {"test_chrome.py": (
        "from browser import session\n"
        "def test_a_test_that_would_start_chrome():\n"
        "    session._launch_chrome('seat')\n"
    )})
    assert done.returncode != 0
    assert "reached the real Chrome spawn" in done.stdout
    assert not root.exists()


def test_the_launch_guard_is_off_for_a_live_test(tmp_path):
    done, root = nested(tmp_path, {"test_live.py": (
        "from browser import session\n"
        "def test_a_live_test_may_launch(monkeypatch):\n"
        "    assert session._launch_chrome.__name__ == '_launch_chrome'\n"
    )}, env={"AR3_TOOL_BROWSER_LIVE": "1"})
    assert done.returncode == 0, done.stdout


def test_the_mixed_command_line_that_once_reached_the_real_root_no_longer_does(tmp_path):
    """The command line that once wrote under the real state directory, on the real tests: files of `browser/`,
    a file from outside it, then a file of `browser/` again. The second visit makes a second collection node for
    `browser/`, and that node has none of the fixtures of a conftest in `browser/`, where the isolation used to
    live. Now the isolation belongs to the session, so nothing reaches the root. The `-k` names are the four
    tests that wrote; "4 passed" proves that all four were found."""
    done, root = nested(
        tmp_path, {},
        args=[
            "browser/test_seatlock.py", "test_runtime.py", "browser/test_session.py",
            "-k", "test_release_empties_the_record or test_not_listed_is_refused"
                  " or test_the_stop_epoch_is_empty_until_bumped_and_changes_each_bump"
                  " or test_the_activity_stamp_is_missing_until_touched_and_then_reads_as_now",
        ],
        cwd=TESTS,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "4 passed" in done.stdout and "failed" not in done.stdout
    assert not (tmp_path / "home" / ".local").exists() and not (tmp_path / "home" / ".config").exists()


def test_the_isolation_does_not_depend_on_the_order_of_the_arguments(tmp_path):
    """The same shape in a project of its own: `sub/` has a conftest with an autouse fixture of its own, as
    `browser/` had, and is visited, left and visited again. The tests of both visits get the plugin's state."""
    isolated = "import os\ndef test_{0}(tmp_path):\n    assert os.environ['XDG_DATA_HOME'] == str(tmp_path / 'xdg')\n"
    project = tmp_path / "project"
    (project / "sub").mkdir(parents=True)
    (project / "sub" / "conftest.py").write_text(
        "import pytest\n@pytest.fixture(autouse=True)\ndef _from_the_directory_conftest(monkeypatch):\n"
        "    monkeypatch.setenv('FROM_SUB', '1')\n"
    )
    for name in ("a", "b"):
        (project / "sub" / f"{name}_test.py").write_text(isolated.format(name))
    (project / "top_test.py").write_text(isolated.format("top"))
    done, _root = nested(tmp_path, {}, args=["sub/a_test.py", "top_test.py", "sub/b_test.py"])
    assert done.returncode == 0, done.stdout + done.stderr
    assert "3 passed" in done.stdout
