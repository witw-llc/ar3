"""Platform launch contracts, including real inherited pipes on Windows."""
import json
import os
import subprocess
import sys

import pytest

from ar3.proc import launch_options

_REAL_RUN = subprocess.run


@pytest.mark.parametrize("new_session", [False, True])
@pytest.mark.parametrize("interactive", [False, True])
def test_windows_policy(new_session, interactive):
    flags = launch_options(windows=True, new_session=new_session, interactive=interactive)
    expected = (0 if interactive else 0x08000000) | (0x200 if new_session else 0)
    assert flags == ({"creationflags": expected} if expected else {})
    assert not flags.get("creationflags", 0) & 0x18


@pytest.mark.parametrize("new_session", [False, True])
def test_windows_breakaway_adds_only_its_flag(new_session):
    plain = launch_options(windows=True, new_session=new_session)["creationflags"]
    away = launch_options(windows=True, new_session=new_session, breakaway=True)
    assert away == {"creationflags": plain | 0x01000000}


def test_posix_breakaway_adds_nothing():
    assert launch_options(windows=False, breakaway=True) == {}
    assert launch_options(windows=False, new_session=True, breakaway=True) == {"start_new_session": True}


@pytest.mark.parametrize("interactive", [False, True])
def test_posix_group_intent(interactive):
    assert launch_options(windows=False, interactive=interactive) == {}
    assert launch_options(windows=False, new_session=True, interactive=interactive) == {"start_new_session": True}


def test_hidden_launch_preserves_pipes_environment_and_exit_status(tmp_path):
    child = "import os,sys; print(os.environ['AR3_LAUNCH_TEST'] + ':' + sys.stdin.read()); sys.exit(7)"
    result = _REAL_RUN(
        [sys.executable, "-c", child], input="payload", capture_output=True,
        text=True, cwd=tmp_path, env={**os.environ, "AR3_LAUNCH_TEST": "value"},
        **launch_options(new_session=True),
    )
    assert result.returncode == 7
    assert result.stdout.strip() == "value:payload"


@pytest.mark.skipif(os.name != "nt", reason="native Windows console handle")
def test_native_windows_hidden_child_has_no_console():
    child = "import ctypes,json; print(json.dumps(bool(ctypes.windll.kernel32.GetConsoleWindow())))"
    result = _REAL_RUN(
        [sys.executable, "-c", child], capture_output=True, text=True, check=True,
        **launch_options(new_session=True),
    )
    assert json.loads(result.stdout) is False


def _windows_proc(monkeypatch, *, returncode=0, raises=None):
    """`ar3.proc` with `os.name == "nt"`: what `terminate_group` runs, and
    what it ends on its own, recorded instead of done."""
    from types import SimpleNamespace

    import ar3.proc as proc

    calls = []

    def run(argv, **kwargs):
        calls.append(("run", argv, kwargs))
        if raises is not None:
            raise raises
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(proc, "os", SimpleNamespace(
        name="nt", kill=lambda pid, sig: calls.append(("kill", pid, sig)),
    ))
    monkeypatch.setattr(proc.subprocess, "run", run)
    return proc, calls


@pytest.mark.parametrize("target", ["popen", "pid"])
def test_windows_terminate_group_ends_the_tree_with_a_hidden_taskkill(monkeypatch, target):
    class Started(subprocess.Popen):
        def __init__(self):
            self.pid = 4321
            self._child_created = False

    proc, calls = _windows_proc(monkeypatch)
    proc.terminate_group(Started() if target == "popen" else 4321)
    assert len(calls) == 1
    _, argv, kwargs = calls[0]
    assert argv == ["taskkill", "/T", "/F", "/PID", "4321"]
    assert kwargs["creationflags"] == 0x08000000
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL


@pytest.mark.parametrize("failure", [{"returncode": 128}, {"raises": OSError("no taskkill")}])
def test_windows_terminate_group_ends_the_process_when_taskkill_cannot(monkeypatch, failure):
    import signal

    proc, calls = _windows_proc(monkeypatch, **failure)
    proc.terminate_group(4321)
    assert calls[-1] == ("kill", 4321, signal.SIGTERM)
