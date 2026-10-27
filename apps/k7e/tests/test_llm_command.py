"""The configured model command runs as a process group that a timeout ends."""
import os
import time

import pytest

import engine
from conftest import write_path_executable

GRANDCHILD = """
import os, subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open(os.environ["K7E_TEST_PIDFILE"], "w") as f:
    f.write(str(child.pid))
child.wait()
"""


def _gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX")
def test_timeout_ends_the_command_and_what_it_forked(store, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_llm_failures", [])
    stub = write_path_executable(tmp_path / "bin", "slow-llm", GRANDCHILD)
    pidfile = tmp_path / "grandchild.pid"
    monkeypatch.setenv("K7E_LLM_COMMAND", str(stub))
    monkeypatch.setenv("K7E_TEST_PIDFILE", str(pidfile))

    assert engine._call_llm("prompt", timeout=1) is None

    grandchild = int(pidfile.read_text())
    deadline = time.monotonic() + 3
    while not _gone(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _gone(grandchild), f"the model process {grandchild} outlived the timeout"
    assert ("summarize", "timed out (1s)") in engine._llm_failures


def test_windows_model_launch_uses_hidden_process_group(store, monkeypatch):
    from ar3.proc import launch_options
    import config

    class LaunchReached(BaseException):
        pass
    calls = []
    def intercept(argv, **kwargs):
        calls.append((argv, kwargs))
        raise LaunchReached
    monkeypatch.setattr(config, "resolve_command", lambda purpose: "fake-model")
    monkeypatch.setattr(engine, "launch_options", lambda **kw: launch_options(windows=True, **kw))
    monkeypatch.setattr(engine.subprocess, "Popen", intercept)
    with pytest.raises(LaunchReached):
        engine._call_llm("prompt")
    assert calls[0][0] == ["fake-model"]
    opts = calls[0][1]
    assert opts["creationflags"] == 0x08000200
    assert not opts.get("start_new_session", False)
    assert opts["stdin"] == engine.subprocess.PIPE
    assert opts["stdout"] == engine.subprocess.PIPE
    assert opts["stderr"] == engine.subprocess.PIPE
    assert opts["encoding"] == "utf-8"
