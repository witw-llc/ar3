"""Exercise launch boundaries with Windows policy without spawning a vendor."""
from __future__ import annotations

import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ar3.proc import launch_options
import engine_memory as memory
import rig
from engines import check, run, values


def force_windows(monkeypatch, module):
    monkeypatch.setattr(module, "launch_options", lambda **kw: launch_options(windows=True, **kw))


@pytest.mark.parametrize("target", ["model_resolution", "model_catalog", "help_probe"])
def test_background_model_and_help_commands_hide_windows_console(monkeypatch, target):
    calls = []

    def capture(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="model-a\n", stderr="")

    module = {"model_resolution": rig, "model_catalog": values, "help_probe": check}[target]
    force_windows(monkeypatch, module)
    monkeypatch.setattr(subprocess, "run", capture)
    if target == "model_resolution":
        assert rig.agy_model_names() == ["model-a"]
    elif target == "model_catalog":
        assert values._lines(["vendor-test", "models"]) == ["model-a"]
    else:
        monkeypatch.setattr(check, "resolve_argv0", lambda argv: argv)
        assert check._run(["vendor-test", "--help"]) == (0, "model-a\n")
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["creationflags"] == 0x08000000
    assert "start_new_session" not in kwargs
    assert kwargs["capture_output"] and kwargs["timeout"] > 0
    if target != "model_resolution":
        assert kwargs["stdin"] == subprocess.DEVNULL


@pytest.mark.real_memory_kick
@pytest.mark.parametrize("target", ["worker", "turn", "distill", "compile"])
def test_memory_children_use_hidden_new_process_groups(monkeypatch, tmp_path, target):
    force_windows(monkeypatch, memory)
    calls = []

    def capture(argv, **kwargs):
        calls.append((argv, kwargs))
        raise OSError("captured launch")

    monkeypatch.setattr(subprocess, "Popen", capture)
    monkeypatch.setattr(memory, "memory_argv", lambda argv: argv)
    capture_path = tmp_path / "turn.json"
    with pytest.raises(OSError, match="captured launch"):
        if target == "worker":
            (tmp_path / "queue").mkdir()
            (tmp_path / "queue" / "turn.json").write_text("{}")
            memory.kick(tmp_path)
        elif target == "turn":
            memory.spawn(["vendor-test"], tmp_path, 3, {"TEST_ENV": "kept"})
        elif target == "distill":
            memory._run_job(tmp_path, capture_path, 3)
        else:
            memory._run_compile(tmp_path, capture_path, 3)
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["creationflags"] == 0x08000200
    assert "start_new_session" not in kwargs
    if target in ("worker", "turn"):
        assert kwargs["stdin"] == subprocess.DEVNULL
    if target == "worker":
        assert kwargs["stdout"] is kwargs["stderr"]
    elif target == "turn":
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["env"] == {"TEST_ENV": "kept"}
    else:
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.PIPE
        assert kwargs["env"]["K7E_HOME"] == str(tmp_path)


@pytest.mark.real_memory_kick
def test_memory_writer_inherits_worker_io_without_opening_console(monkeypatch, tmp_path):
    force_windows(monkeypatch, memory)
    capture = tmp_path / "turns" / "turn.json"
    capture.parent.mkdir()
    capture.write_text(json.dumps({"writer": {"engine": "claude", "model": None}}))
    monkeypatch.setattr(memory, "os", SimpleNamespace(name="nt", environ={"TEST_ENV": "kept"},
                                                     chdir=lambda path: None))
    monkeypatch.setattr(memory.sys, "stdin", io.StringIO("bounded input"))
    monkeypatch.setattr(run, "build_argv", lambda *args, **kwargs: ["vendor-test", "bounded input"])
    monkeypatch.setattr(memory, "memory_argv", lambda argv: argv)
    calls = []
    monkeypatch.setattr(subprocess, "call", lambda argv, **kwargs: calls.append((argv, kwargs)) or 7)
    assert memory.writer(capture) == 7
    assert calls == [(["vendor-test", "bounded input"],
                      {"env": {"TEST_ENV": "kept"}, "creationflags": 0x08000000})]


def test_memory_tree_cleanup_is_the_foundation_group_kill(monkeypatch):
    """The Windows `taskkill /T` arm and its hidden console are the
    foundation's (apps/ar3/tests/test_ar3_proc.py); r4t keeps no copy."""
    calls = []
    monkeypatch.setattr(memory, "terminate_group", lambda proc, **kw: calls.append(proc.pid))
    memory.stop_process(SimpleNamespace(pid=1234, poll=lambda: 0))
    assert calls == [1234]


@pytest.mark.skipif(sys.platform == "win32", reason="Docker bundle uses a POSIX symlink")
def test_docker_bundle_layout_bootstraps_foundation_through_app_symlink(tmp_path):
    source = Path(__file__).resolve().parents[1]
    bundle = tmp_path / "opt" / "ar3"
    app = bundle / "apps" / "r4t"
    shutil.copytree(source, app, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    shutil.copytree(source.parents[1] / "lib", bundle / "lib",
                    ignore=shutil.ignore_patterns("__pycache__"))
    alias = tmp_path / "opt" / "r4t"
    alias.symlink_to(app, target_is_directory=True)
    result = subprocess.run([sys.executable, "-E", "-s", str(alias / "r4t.py"), "--version"],
                            capture_output=True, text=True, timeout=15, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "r4t unknown" in result.stdout
