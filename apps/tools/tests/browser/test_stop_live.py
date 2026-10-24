"""A stop ends the script that holds the seat. Live: needs Chrome and playwright-cli."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("AR3_TOOL_BROWSER_LIVE") != "1",
    reason="live: set AR3_TOOL_BROWSER_LIVE=1",
)

TOOL = Path(__file__).resolve().parents[2] / "browser"


def _profile_processes(profile):
    out = subprocess.run(
        ["pgrep", "-af", "--", f"--user-data-dir={profile}"],
        capture_output=True, text=True,
    )
    return out.stdout.strip()


def test_a_stop_during_a_script_leaves_no_chrome(tmp_path):
    seat = f"ar3scratch{os.getpid()}"
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    env.update(
        XDG_DATA_HOME=str(tmp_path / "data"),
        A8S_HOME=str(tmp_path / "a8s"),
        AR3_BROWSER_SEAT_WAIT="1",
    )
    tool = [sys.executable, str(TOOL)]
    script = tmp_path / "script"
    script.write_text("wait 12\nopen\n")
    out = open(tmp_path / "out", "w")
    err = open(tmp_path / "err", "w")
    holder = subprocess.Popen(
        tool + ["--seat", seat, "do", str(script), "--json"],
        env=env, stdout=out, stderr=err,
    )
    profile = tmp_path / "data/ar3/browser" / seat / "chrome"
    try:
        # `wait` runs after ensure_running, so the script launches Chrome itself.
        deadline = time.monotonic() + 40
        while not (profile / "DevToolsActivePort").exists() and time.monotonic() < deadline:
            assert holder.poll() is None, (tmp_path / "out").read_text()
            time.sleep(0.2)
        assert (profile / "DevToolsActivePort").exists()
        time.sleep(1)
        assert holder.poll() is None
        closed = subprocess.run(
            tool + ["close", "--seat", seat], env=env,
            capture_output=True, text=True, timeout=60,
        )
        assert closed.returncode == 0, closed.stderr
        holder.wait(timeout=40)
        transcript = (tmp_path / "out").read_text()
        assert holder.returncode == 1, transcript
        assert '"command": "open", "ok": false' in transcript
        assert "seat closed by a8s stop" in transcript
        time.sleep(3)
        assert _profile_processes(profile) == ""
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()
        out.close()
        err.close()
        subprocess.run(tool + ["close", "--seat", seat], env=env,
                       capture_output=True, text=True, timeout=60)


def test_a_stop_that_gave_up_on_the_launch_lock_leaves_no_chrome(tmp_path):
    """The launch passes its epoch check and stalls before the spawn. The stop
    times out on the launch lock, bumps the epoch and finds no Chrome. The
    launch then spawns, re-reads the epoch and ends its own Chrome."""
    seat = f"ar3scratch{os.getpid()}b"
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    env.update(
        XDG_DATA_HOME=str(tmp_path / "data"),
        A8S_HOME=str(tmp_path / "a8s"),
        PYTHONPATH=os.pathsep.join([str(TOOL.parent), str(TOOL.parents[2] / "lib")]),
    )
    barrier = tmp_path / "barrier"
    barrier.write_text("x")
    code = f"""
import os, sys, time
from browser import session
def hook():
    print("at hook", flush=True)
    while os.path.exists({str(barrier)!r}):
        time.sleep(0.1)
session._before_spawn = hook
try:
    session.ensure_running({seat!r}, script_epoch=session.stop_epoch({seat!r}))
    print("launched")
except session.plc.BrowserError as exc:
    print("error:", exc)
"""
    profile = tmp_path / "data/ar3/browser" / seat / "chrome"
    launcher = subprocess.Popen(
        [sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, text=True,
        cwd=str(tmp_path),
    )
    tool = [sys.executable, str(TOOL)]
    try:
        assert launcher.stdout.readline().strip() == "at hook"
        started = time.monotonic()
        closed = subprocess.run(
            tool + ["close", "--seat", seat], env=env,
            capture_output=True, text=True, timeout=60,
        )
        assert closed.returncode == 0, closed.stderr
        assert "closing anyway" in closed.stderr
        assert time.monotonic() - started < 15
        barrier.unlink()
        out = launcher.communicate(timeout=60)[0]
        assert "error: seat closed by a8s stop" in out
        time.sleep(3)
        assert _profile_processes(profile) == ""
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait()
        subprocess.run(tool + ["close", "--seat", seat], env=env,
                       capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("mode", ["repair", "cliopen"])
def test_other_launch_paths_leave_no_chrome_after_a_stop(tmp_path, mode):
    """The windowless-page restart inside a script, and the direct CLI `open`,
    both paused before they act. A stop lands. They resume and must not leave Chrome."""
    seat = f"ar3scratch{os.getpid()}{mode}"
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    env.update(
        XDG_DATA_HOME=str(tmp_path / "data"),
        A8S_HOME=str(tmp_path / "a8s"),
        PYTHONPATH=os.pathsep.join([str(TOOL.parent), str(TOOL.parents[2] / "lib")]),
    )
    tool = [sys.executable, str(TOOL)]
    barrier = tmp_path / "barrier"
    barrier.write_text("wait")
    code = f"""
import os, sys, time, io
from browser import session, cli
seat = {seat!r}
def barrier():
    print("at barrier", flush=True)
    while os.path.exists({str(barrier)!r}):
        time.sleep(0.05)
"""
    if mode == "repair":
        ready = subprocess.run(tool + ["--seat", seat, "open"], env=env,
                               capture_output=True, text=True, timeout=60)
        assert ready.returncode == 0, ready.stderr
        code += """
def window(s):
    barrier()
    return {"window": None, "url": "about:blank", "visible": "visible"}
session._window_of_page = window
sys.argv = ["browser", "--seat", seat, "do", "-", "--json"]
sys.stdin = io.StringIO("url" + chr(10))
sys.exit(cli.main())
"""
    else:
        code += """
original = session._launch_chrome
def launch(s):
    barrier()
    return original(s)
session._launch_chrome = launch
sys.argv = ["browser", "--seat", seat, "open"]
sys.exit(cli.main())
"""
    profile = tmp_path / "data/ar3/browser" / seat / "chrome"
    holder = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, cwd=str(tmp_path))
    try:
        assert holder.stdout.readline().strip() == "at barrier"
        closed = subprocess.run(tool + ["close", "--seat", seat], env=env,
                                capture_output=True, text=True, timeout=60)
        assert closed.returncode == 0, closed.stderr
        barrier.unlink()
        out, err = holder.communicate(timeout=60)
        assert holder.returncode == 1, (out, err)
        assert "seat closed by a8s stop" in out + err
        time.sleep(3)
        assert _profile_processes(profile) == ""
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()
        subprocess.run(tool + ["close", "--seat", seat], env=env,
                       capture_output=True, text=True, timeout=60)


def test_a_do_waiting_for_the_seat_when_the_stop_lands_leaves_no_chrome(tmp_path):
    seat = f"ar3scratch{os.getpid()}w"
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    env.update(XDG_DATA_HOME=str(tmp_path / "data"), A8S_HOME=str(tmp_path / "a8s"),
               AR3_BROWSER_SEAT_WAIT="60")
    tool = [sys.executable, str(TOOL)]
    first = tmp_path / "first"
    first.write_text("wait 6\n")
    second = tmp_path / "second"
    second.write_text("open\n")
    profile = tmp_path / "data/ar3/browser" / seat / "chrome"
    holder = subprocess.Popen(tool + ["--seat", seat, "do", str(first), "--json"], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    waiter = None
    try:
        deadline = time.monotonic() + 40
        while not (profile / "DevToolsActivePort").exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        waiter = subprocess.Popen(tool + ["--seat", seat, "do", str(second), "--json"], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(1.5)
        assert waiter.poll() is None
        closed = subprocess.run(tool + ["close", "--seat", seat], env=env,
                                capture_output=True, text=True, timeout=60)
        assert closed.returncode == 0, closed.stderr
        holder.communicate(timeout=60)
        out, err = waiter.communicate(timeout=60)
        assert waiter.returncode == 1, (out, err)
        assert "seat closed by a8s stop" in out
        time.sleep(3)
        assert _profile_processes(profile) == ""
        # Control: a do that begins after the stop opens Chrome again.
        after = subprocess.run(tool + ["--seat", seat, "do", str(second), "--json"], env=env,
                               capture_output=True, text=True, timeout=60)
        assert after.returncode == 0, (after.stdout, after.stderr)
    finally:
        for proc in (holder, waiter):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()
        subprocess.run(tool + ["close", "--seat", seat], env=env,
                       capture_output=True, text=True, timeout=60)
