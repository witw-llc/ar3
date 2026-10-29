"""Real pipe backpressure must not strand resident fatal/stop teardown."""
from __future__ import annotations

import array
import sys
import threading
import time

import pytest

from engines import resident as resident_mod
from engines import run as engine_run


@pytest.mark.skipif(sys.platform != "linux", reason="measures Linux pipe occupancy")
@pytest.mark.parametrize("cause", ["fatal", "stop", "deadline", "reader-deadline"])
def test_full_reconciliation_pipe_exits_without_rescue(monkeypatch, tmp_path, cause):
    import fcntl
    import termios

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("A8S_HOME", str(tmp_path / "a8s"))
    monkeypatch.delenv("TELL_OUTBOX_DIR", raising=False)
    monkeypatch.setattr(resident_mod, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(resident_mod, "STDIN_WRITE_SECONDS", 0.5 if "deadline" in cause else 5)
    fake = tmp_path / "blocked-server.py"
    fake.write_text('''import json, sys, time
from pathlib import Path
root = Path(sys.argv[1])
def send(frame): print(json.dumps(frame), flush=True)
def event(method, **params): send({'method': method, 'params': {'threadId': 'parent', **params}})
parent_turn, child = 't' * 36, 'c' * 36
for line in sys.stdin:
    frame = json.loads(line)
    if frame.get('method') == 'initialize':
        send({'id': frame['id'], 'result': {'userAgent': 'codex/0.163.0'}})
    elif frame.get('method') == 'thread/start':
        send({'id': frame['id'], 'result': {'thread': {'id': 'parent'}, 'approvalPolicy': 'never', 'sandbox': {'type': 'workspaceWrite'}}})
    elif frame.get('method') == 'turn/start':
        send({'id': frame['id'], 'result': {'turn': {'id': parent_turn}}})
        if sys.argv[2] == 'reader-deadline':
            event('turn/completed', turn={'id': parent_turn, 'status': 'completed', 'items': []})
            send({'id': 'r' * 16384, 'method': 'item/commandExecution/requestApproval', 'params': {}})
            (root / 'burst').touch()
            time.sleep(60)
            break
        event('item/completed', turnId=parent_turn, item={'type': 'subAgentActivity', 'id': 'spawn', 'kind': 'started', 'agentThreadId': child})
        for i in range(32):
            event('item/completed', turnId=parent_turn, item={'type': 'subAgentActivity', 'id': str(i).zfill(36), 'kind': 'completed', 'agentThreadId': child})
        event('turn/completed', turn={'id': parent_turn, 'status': 'completed', 'items': []})
        (root / 'burst').touch()
        while not (root / 'fatal').exists(): time.sleep(.01)
        send([])
        (root / 'fatal-sent').touch()
        time.sleep(60)
''')
    home = tmp_path / "resident"
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "initial", None)
    monkeypatch.setattr(engine_run, "resolve_argv0", lambda argv: [sys.executable, str(fake), str(tmp_path), cause])
    processes, sessions, writers = [], [], []
    popen = resident_mod.subprocess.Popen
    init_writer = resident_mod._ResidentStdin.__init__
    on_frame = resident_mod.CodexSession.on_frame

    def small_pipe(*args, **kwargs):
        proc = popen(*args, **kwargs)
        assert fcntl.fcntl(proc.stdin, fcntl.F_SETPIPE_SZ, 4096) == 4096
        processes.append(proc)
        return proc

    def observe_writer(self, *args):
        init_writer(self, *args)
        writers.append(self)

    def observe_frame(self, frame):
        if not sessions:
            sessions.append(self)
        return on_frame(self, frame)

    monkeypatch.setattr(resident_mod.subprocess, "Popen", small_pipe)
    monkeypatch.setattr(resident_mod._ResidentStdin, "__init__", observe_writer)
    monkeypatch.setattr(resident_mod.CodexSession, "on_frame", observe_frame)
    template, _ = engine_run._build_argv_template("codex", model=None, timeout=30,
                                                workdir=tmp_path, resident=True)
    stop = threading.Event()
    results, errors = [], []
    current = {}

    def run():
        try:
            results.append(resident_mod._run_session(home, {
                "engine": "codex", "dir_path": str(tmp_path), "template": template,
                "agent": "probe", "timeout": 10, "scaffold": False, "memory": "off",
            }, stop, current))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 4
        occupancy = array.array("i", [0])
        while time.monotonic() < deadline:
            if processes and (tmp_path / "burst").exists():
                fcntl.ioctl(processes[0].stdin.fileno(), termios.FIONREAD, occupancy, True)
                if occupancy[0] == 4096:
                    break
            time.sleep(0.01)
        assert occupancy[0] == 4096, f"test did not fill the pipe: {errors}"
        assert worker.is_alive() and writers[0].worker.is_alive()
        started = time.monotonic()
        if cause == "fatal":
            (tmp_path / "fatal").touch()
        elif cause == "stop":
            stop.set()
        worker.join(4)
        assert not worker.is_alive(), "resident needed rescue while stdin was full"
        assert time.monotonic() - started < 4
        assert errors == [] and results == [1]
        assert processes[0].poll() is not None
        assert not writers[0].worker.is_alive()
        assert processes[0].stdin.closed
        assert current["proc"] is None
        assert resident_mod.read_state(home)["turns"] == 0
        assert sessions[0].continuation_count == (0 if cause == "reader-deadline" else 1)
        assert not sessions[0].pending_continuation()
        if cause == "fatal":
            assert (tmp_path / "fatal-sent").exists()
    finally:
        # Rescue is cleanup only. Assertions above must pass before it runs.
        stop.set()
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
        worker.join(3)


@pytest.mark.parametrize("missing_stdin", [True, False])
def test_failed_writer_rejects_competitor_at_slot_release(missing_stdin):
    from io import StringIO
    from types import SimpleNamespace

    class BrokenStream:
        def write(self, text):
            raise BrokenPipeError("closed")

    proc = SimpleNamespace(stdin=None if missing_stdin else BrokenStream(), poll=lambda: None)
    writer = resident_mod._ResidentStdin(proc, threading.Event(), threading.Event())
    later_stream = StringIO()
    raced = threading.Event()
    results = []

    class RacingSlot:
        released = False

        def acquire(self, timeout):
            return True

        def release(self):
            if not self.released:
                self.released = True
                proc.stdin = later_stream
                results.append(writer.write(["must not be sent"]))
                raced.set()

    writer.slot = RacingSlot()
    assert not writer.write(["first"])
    assert raced.wait(1)
    writer.finish()
    assert results == [False]
    assert later_stream.getvalue() == ""
