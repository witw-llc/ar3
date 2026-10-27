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
