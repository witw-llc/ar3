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

    # `engine check` probes through the launch, whose probe lives in rig.py.
    module = {"model_resolution": rig, "model_catalog": values, "help_probe": rig}[target]
    force_windows(monkeypatch, module)
    monkeypatch.setattr(subprocess, "run", capture)
    if target == "model_resolution":
        assert rig.agy_model_names(launch=rig.Launch("agy", "/fake/agy", {}, "/")) == ["model-a"]
    elif target == "model_catalog":
        assert values._lines(["vendor-test", "models"]) == ["model-a"]
    else:
        launch = rig.Launch("vendor-test", "/fake/vendor-test", {}, "/")
        assert check._run(launch, ["--help"]) == (0, "model-a\n")
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
    # The memory turn starts through Launch.start, which applies the policy in rig.
    force_windows(monkeypatch, rig)
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
            memory.spawn(
                rig.Launch("vendor-test", "/fake/vendor-test", {"TEST_ENV": "kept"}, str(tmp_path)),
                ["vendor-test"], 3,
            )
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
    from conftest import write_path_executable

    vendor = write_path_executable(tmp_path / "bin", "vendor-test", "pass\n")
    environ = {"TEST_ENV": "kept", "PATH": str(tmp_path / "bin")}
    monkeypatch.setattr(memory, "os", SimpleNamespace(name="nt", environ=environ,
                                                     chdir=lambda path: None))
    monkeypatch.setattr(memory.sys, "stdin", io.StringIO("bounded input"))
    monkeypatch.setattr(run, "build_argv", lambda *args, **kwargs: ["vendor-test", "bounded input"])
    monkeypatch.setattr(memory, "memory_argv", lambda argv: argv)
    calls = []
    monkeypatch.setattr(subprocess, "call", lambda argv, **kwargs: calls.append((argv, kwargs)) or 7)
    assert memory.writer(capture) == 7
    # The launch resolved the file on the child's PATH and starts that path, in
    # the writer's directory, with exactly the environment it was built from.
    assert calls == [([str(vendor), "bounded input"],
                      {"env": environ, "cwd": str(tmp_path / "writer"),
                       "creationflags": 0x08000000})]


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


@pytest.mark.parametrize("windows", [True, False])
def test_engine_run_spawn_goes_through_launch_start_once(monkeypatch, tmp_path, windows):
    """engines.run._spawn -> Launch.start with the launch policy active at both
    sites: one Popen, with Windows flags on Windows and a new session on POSIX.
    A caller that also passed launch_options would double creationflags."""
    if windows:
        force_windows(monkeypatch, rig)
        # engines.run no longer applies a policy of its own; if it ever imports
        # launch_options again, force Windows there too so a double apply shows.
        if hasattr(run, "launch_options"):
            force_windows(monkeypatch, run)
    calls = []

    def capture(argv, **kwargs):
        calls.append(kwargs)
        raise OSError("captured launch")

    monkeypatch.setattr(subprocess, "Popen", capture)
    launch = rig.Launch("vendor-test", "/fake/vendor-test", {"TEST_ENV": "kept"}, str(tmp_path))
    with pytest.raises(run.RunError, match="captured launch"):
        run._spawn(launch, ["vendor-test", "-p", "x"], 3)
    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["env"] == {"TEST_ENV": "kept"} and kwargs["cwd"] == str(tmp_path)
    if windows:
        assert kwargs["creationflags"] == 0x08000200
        assert "start_new_session" not in kwargs
    else:
        assert kwargs == {**kwargs, **launch_options(new_session=True, windows=False)}


def test_launch_start_refuses_policy_keys_from_a_caller(tmp_path):
    launch = rig.Launch("vendor-test", "/fake/vendor-test", {}, str(tmp_path))
    for key in ("creationflags", "start_new_session", "startupinfo", "process_group"):
        with pytest.raises(TypeError, match="Launch.start owns"):
            launch.start(["vendor-test"], **{key: 0})


def test_no_launch_start_caller_passes_launch_options():
    """Source guard: Launch.start applies the policy itself, so no caller in the
    product tree may splat launch_options into it."""
    import ast
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in root.rglob("*.py"):
        if "tests" in path.parts or "experiments" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "start"):
                for kw in node.keywords:
                    if kw.arg is None and isinstance(kw.value, ast.Call) and "launch_options" in ast.unparse(kw.value.func):
                        offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []
