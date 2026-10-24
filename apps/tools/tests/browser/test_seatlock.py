import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from ar3.proc import spawn

from browser import seatlock

TOOLS = str(Path(__file__).resolve().parents[2])
LIB = str(Path(__file__).resolve().parents[4] / "lib")

HOLDER = (
    "import sys, time\n"
    "from browser import seatlock\n"
    "with seatlock.hold('t', 'a8s wake', sender='iris'):\n"
    "    print('held', flush=True)\n"
    "    time.sleep(60)\n"
)

TRY = (
    "import sys\n"
    "from browser import seatlock\n"
    "try:\n"
    "    with seatlock.hold('t', 'browser cli'):\n"
    "        print('got')\n"
    "except seatlock.SeatBusy as busy:\n"
    "    print('busy', busy)\n"
)


def _env(monkeypatch, extra=None):
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = f"{TOOLS}:{LIB}"
    env.update(extra or {})
    return env


def _start_holder(env):
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER], env=env, stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout.readline().strip() == "held"
    return proc


@pytest.fixture
def env(monkeypatch):
    return _env(monkeypatch)


def test_a_second_holder_is_told_who_holds_the_seat(env):
    first = _start_holder(env)
    try:
        started = time.monotonic()
        out = subprocess.run(
            [sys.executable, "-c", TRY],
            env={**env, "AR3_BROWSER_SEAT_WAIT": "0.5"},
            capture_output=True, text=True,
        )
        assert time.monotonic() - started < 3
        assert out.stdout.startswith("busy seat held by a8s wake from iris")
        assert f"(pid {first.pid})" in out.stdout
    finally:
        first.kill()
        first.wait()


def test_a_killed_holder_frees_the_seat(env):
    first = _start_holder(env)
    first.kill()
    first.wait()
    with seatlock.hold("t", "browser cli", wait=2):
        pass


def test_release_empties_the_record():
    with seatlock.hold("t", "browser cli"):
        assert json.loads(seatlock.lock_path("t").read_text())["kind"] == "browser cli"
    assert seatlock.lock_path("t").read_text() == ""
    assert seatlock.holder("t") is None


def test_a_record_of_a_dead_process_reads_as_another_process():
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    path = seatlock.lock_path("t")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": dead.pid, "start": "x", "kind": "a8s wake"}))
    assert seatlock.holder("t") is None
    assert seatlock.describe(seatlock.holder("t")) == "another process"


def test_a_child_started_while_holding_does_not_keep_the_seat(env):
    with seatlock.hold("t", "browser cli"):
        child = spawn(["sleep", "30"], cwd="/")
    try:
        out = subprocess.run(
            [sys.executable, "-c", TRY],
            env={**env, "AR3_BROWSER_SEAT_WAIT": "1"},
            capture_output=True, text=True,
        )
        assert out.stdout.strip() == "got"
    finally:
        child.kill()
        child.wait()


def test_on_wait_is_called_once(env):
    first = _start_holder(env)
    told = []
    try:
        with pytest.raises(seatlock.SeatBusy):
            with seatlock.hold("t", "browser cli", wait=0.7, on_wait=told.append):
                pass
    finally:
        first.kill()
        first.wait()
    assert len(told) == 1
    assert told[0].startswith("a8s wake from iris")


def test_the_wait_comes_from_the_environment(monkeypatch):
    assert seatlock.seat_wait() == seatlock.SEAT_WAIT_SECONDS
    monkeypatch.setenv("AR3_BROWSER_SEAT_WAIT", "0.25")
    assert seatlock.seat_wait() == 0.25
