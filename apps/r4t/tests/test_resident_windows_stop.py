"""Cooperative state requests on forced Windows branches; fake children only."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import nullcontext

import pytest

from ar3.proc import launch_options
from ar3.wake_outcome import origin_from_env, take_failures
from engines import resident as r


@pytest.fixture
def windows_bridge(monkeypatch):
    monkeypatch.setattr(r, "IS_WINDOWS", True)
    monkeypatch.setattr(r, "_use_windows_stop", lambda: True)
    # Exercise native-like terminate/wait/kill on a POSIX test host, while
    # retaining the host's real launch flags. No vendor executable is used.
    monkeypatch.setattr(r, "launch_options", lambda **kw: launch_options(
        new_session=kw.get("new_session", False)))
    monkeypatch.setattr(r.signal, "signal", lambda *args: None)


def _watchers():
    return [t for t in threading.enumerate() if t.name == "resident-stop-watcher"]


def test_windows_watcher_rejects_other_generation_and_joins(tmp_path, monkeypatch, windows_bridge):
    r._spec_file(tmp_path).write_text('{}', encoding='utf-8')
    seen = []

    def session(home, spec, stop, current):
        r._write_state(home, pid=os.getpid() + 1, engine_pid=None, status="stopping")
        assert not stop.wait(0.15)
        seen.extend(_watchers())
        r._write_state(home, pid=os.getpid(), engine_pid=None, status="stopping")
        assert stop.wait(1)
        return 0

    monkeypatch.setattr(r, "_run_session", session)
    assert r.serve_resident(tmp_path) == 0
    assert len(seen) == 1
    assert not seen[0].is_alive()
    assert not _watchers()


@pytest.mark.parametrize("failure", ["spec", "session"])
def test_windows_watcher_joins_on_error(tmp_path, monkeypatch, windows_bridge, failure):
    if failure == "session":
        r._spec_file(tmp_path).write_text('{}', encoding='utf-8')
        def broken(*args):
            raise RuntimeError("synthetic failure")
        monkeypatch.setattr(r, "_run_session", broken)
        with pytest.raises(RuntimeError, match="synthetic failure"):
            r.serve_resident(tmp_path)
    else:
        assert r.serve_resident(tmp_path) == 1
    assert not _watchers()


@pytest.mark.parametrize("phase", ["active", "handshake", "wrapup", "eof"])
def test_windows_state_stop_interrupts_real_session_waits(
    tmp_path, monkeypatch, windows_bridge, phase,
):
    home = r.resident_home(tmp_path, "probe")
    home.mkdir(parents=True)
    marker = tmp_path / "waiting"
    fake = tmp_path / "fake.py"
    fake.write_text(
        "import json, sys, time, threading, os\n"
        "from pathlib import Path\n"
        "threading.Timer(6, lambda: os._exit(90)).start()\n"
        f"phase = {phase!r}\nmarker = Path({str(marker)!r})\n"
        "for i, line in enumerate(sys.stdin):\n"
        "    if phase in ('active', 'handshake') or (phase == 'wrapup' and i == 1):\n"
        "        marker.write_text('waiting', encoding='utf-8')\n"
        "        time.sleep(6)\n"
        "    print(json.dumps({'event':'result','result':{'response':'ok'}}), flush=True)\n"
        "marker.write_text('eof', encoding='utf-8')\n"
        "time.sleep(6)\n",
        encoding="utf-8",
    )
    origin = origin_from_env({
        'A8S_TURN_RECIPIENT': 'probe',
        'A8S_TURN_ENVELOPES': '["original.json"]',
        'A8S_TURN_OUTCOME_DIR': str(tmp_path / 'failures'),
    }, 'probe')
    r._enqueue_message(home, "hello", origin)
    r._write_pid_files(home, os.getpid())
    r._write_state(home, pid=os.getpid(), engine_pid=None, status="starting")
    r._spec_file(home).write_text(json.dumps({
        'engine': 'cursor' if phase == 'handshake' else 'agy',
        'dir_path': str(tmp_path), 'template': [sys.executable, str(fake)],
        'agent': 'probe', 'timeout': 1, 'scaffold': phase == 'wrapup',
    }), encoding='utf-8')
    monkeypatch.setattr(r, "compose_resident_argv", lambda template, *a, **kw: template)
    calls = []
    real_report = r.report_failure
    monkeypatch.setattr(r, "report_failure", lambda *a, **kw: (
        calls.append((a, kw)), real_report(*a, **kw))[1])
    stopped_at = []
    controller_errors = []

    def request_stop():
        deadline = time.monotonic() + 4
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not marker.exists():
            controller_errors.append("fake did not reach requested phase")
        with r.file_lock(r._lock_file(home)):
            state = r.read_state(home) or {}
            stopped_at.append(time.monotonic())
            r._write_state(home, pid=os.getpid(), engine_pid=state.get('engine_pid'),
                           status='stopping')

    controller = threading.Thread(target=request_stop)
    controller.start()
    try:
        code = r.serve_resident(home)
    finally:
        controller.join(timeout=5)
    assert not controller.is_alive()
    assert not controller_errors
    assert time.monotonic() - stopped_at[0] < 1.5
    assert not _watchers()
    assert r.read_live_pid(home) is None
    records = list(take_failures(tmp_path / 'failures', 'probe'))
    if phase in ('active', 'handshake'):
        assert code == 1
        assert len(calls) == len(records) == 1
        assert records[0]['envelopes'] == ['original.json']
        assert records[0]['reason'] == 'stop'
    elif phase == 'wrapup':
        assert code == 1
        assert len(calls) == len(records) == 1
        assert records[0]['envelopes'] == []  # completed original stays complete
    else:
        assert code == 0
        assert calls == records == []


@pytest.mark.parametrize("held,windows", [(True, True), (False, True), (True, False)])
def test_windows_stop_has_bounded_fallback_and_preserves_posix_signal(
    tmp_path, monkeypatch, held, windows,
):
    monkeypatch.setattr(r, 'IS_WINDOWS', windows)
    monkeypatch.setattr(r, '_try_lock', lambda *a: nullcontext() if held else None)
    monkeypatch.setattr(r, 'file_lock', lambda *a: nullcontext())
    monkeypatch.setattr(r, 'read_live_pid', lambda *a: 12345)
    monkeypatch.setattr(r, 'read_state', lambda *a: {'pid':12345, 'engine_pid':67890})
    calls = []
    for name in ('_write_state', 'terminate_group', '_sweep_group',
                 '_end_published_engine', '_fail_inbox', '_clear_pid_files'):
        monkeypatch.setattr(r, name, lambda *a, _name=name, **kw: calls.append((_name, a, kw)))
    monkeypatch.setattr(r.os, 'kill', lambda *a: calls.append(('signal', a, {})))
    started = time.monotonic()
    assert r.stop_resident(tmp_path, 'probe', grace_seconds=0.1) is True
    elapsed = time.monotonic() - started
    names = [call[0] for call in calls]
    assert ('signal' in names) == (held and not windows)
    assert ('_write_state' in names) == held
    assert 'terminate_group' in names
    assert elapsed < 0.5
    if held:
        assert elapsed >= 0.1
    assert calls[-1] == ('_clear_pid_files', (r.resident_home(tmp_path, 'probe'),),
                         {'expected_pid':12345})


def test_windows_stop_leaves_replacement_generation_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(r, 'IS_WINDOWS', True)
    monkeypatch.setattr(r, '_try_lock', lambda *a: nullcontext())
    monkeypatch.setattr(r, 'file_lock', lambda *a: nullcontext())
    pids = iter([12345, 54321, 54321, 54321])
    monkeypatch.setattr(r, 'read_live_pid', lambda *a: next(pids))
    monkeypatch.setattr(r, 'read_state', lambda *a: {'pid':12345})
    monkeypatch.setattr(r, '_write_state', lambda *a, **kw: None)
    monkeypatch.setattr(r, '_sweep_group', lambda *a: None)
    for name in ('terminate_group', '_end_published_engine', '_fail_inbox', '_clear_pid_files'):
        def unexpected(*a, _name=name, **kw):
            pytest.fail(f'{_name} touched replacement')
        monkeypatch.setattr(r, name, unexpected)
    monkeypatch.setattr(r.os, 'kill', lambda *a: pytest.fail('Windows SIGTERM'))
    assert r.stop_resident(tmp_path, 'probe', grace_seconds=0.1) is True
