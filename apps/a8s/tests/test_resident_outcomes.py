"""Detached failures settle locally after an enqueue has already succeeded."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import daemon
from ar3.wake_outcome import (
    origin_from_env, outcome_dir, report_failure,
)
from core import Participant, agent_dir, dead_letters_dir, read_last_wake, trash_dir
from mailbox import ensure_mailboxes
import txlog


@pytest.fixture
def node(fake_home, tmp_path):
    root = tmp_path / "seat"
    root.mkdir()
    p = Participant("probe", root)
    ensure_mailboxes(p)
    txlog.log("RUN_START", sender=p.name)
    return p


def envelope(node, name):
    path = trash_dir(node.name) / (name + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{}')
    return path


def origin(node, files):
    env = daemon._wake_env(node, {}, files)
    return origin_from_env(env, node.name)


def failed_events():
    return [e for _, e in txlog.read_recent(limit=1000) if e['event'] == 'WAKE_FAILED']


def test_failure_waits_for_enqueue_settlement_and_is_consumed_once(node, monkeypatch):
    file = envelope(node, 'one')
    context = origin(node, [file])
    report_failure(context, 'auth required')
    monkeypatch.setattr(daemon, '_CURRENT_WAKE_NAME', node.name)
    daemon._consume_wake_failures(node.name)
    assert not (dead_letters_dir(node.name) / file.name).exists()
    daemon._settle_wake(node, [file], 0)
    monkeypatch.setattr(daemon, '_CURRENT_WAKE_NAME', None)
    daemon._consume_wake_failures(node.name)
    assert (dead_letters_dir(node.name) / file.name).exists()
    assert read_last_wake(node.name) is False
    assert len(failed_events()) == 1
    report_failure(context, 'same origin after a killed enqueuer')
    daemon._consume_wake_failures(node.name)
    assert len(failed_events()) == 1


def test_consumer_rejects_other_recipient_and_traversal(node):
    directory = outcome_dir(agent_dir(node.name))
    directory.mkdir(parents=True, exist_ok=True)
    for i, record in enumerate((
        {'id': 'x', 'recipient': 'other', 'envelopes': ['x.json'], 'reason': 'bad'},
        {'id': 'x', 'recipient': node.name, 'envelopes': ['../x.json'], 'reason': 'bad'},
        {'id': 'x', 'recipient': node.name, 'envelopes': ['C:x.json'], 'reason': 'bad'},
        {'id': 'x', 'recipient': node.name, 'envelopes': ['..\\x.json'], 'reason': 'bad'},
    )):
        (directory / f'{i}.json').write_text(json.dumps(record))
    daemon._consume_wake_failures(node.name)
    assert not failed_events()
    assert not list(directory.iterdir())
    env = daemon._wake_env(node, {}, [])
    assert origin_from_env(env, 'other') is None


def test_session_failure_has_no_dead_letters(node):
    file = envelope(node, 'already-completed')
    report_failure(origin(node, [file]), 'failed wrap-up', session=True)
    daemon._consume_wake_failures(node.name)
    assert not (dead_letters_dir(node.name) / file.name).exists()
    assert len(failed_events()) == 1


# Keep r4t imports in a separate interpreter: the applications have modules
# with the same names. The helper owns/reaps its bridge and fake engine.
RUNNER = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
repo, root, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path[:0] = [str(repo/'lib'), str(repo/'apps/r4t')]
from engines import resident as r
from ar3.wake_outcome import origin_from_env
home = r.resident_home(root, 'probe')
def wait(pred):
    end = time.monotonic()+8
    while time.monotonic()<end:
        if pred(): return
        time.sleep(.03)
    raise AssertionError('timeout')
fake = root/'fake-claude'
fake.write_text('#!'+sys.executable+'\n'+"""import json, os, sys, time
from pathlib import Path
assert not any(k.startswith('A8S_TURN_') for k in os.environ)
for n, line in enumerate(sys.stdin):
    if Path('mode').read_text() == 'second' and n == 0:
        print(json.dumps({'type':'result','result':'ok'}), flush=True)
        Path('first-done').touch()
        continue
    if Path('mode').read_text() == 'exit':
        print('authentication required', flush=True)
        sys.exit(1)
    if Path('mode').read_text() == 'stop':
        Path('started').touch()
        time.sleep(30)
    print(json.dumps({'type':'result','is_error':True,'errors':['auth required']}), flush=True)
    time.sleep(30)
""")
fake.chmod(0o700)
(root/'mode').write_text(mode)
original_env = dict(os.environ)
files = json.loads(original_env['A8S_TURN_ENVELOPES'])
results=[]
bridge = None
bridge_proc = None
def enqueue(paths):
    env=dict(original_env, A8S_TURN_ENVELOPES=json.dumps(paths))
    rc=r.enqueue_or_start('claude','hello',dir_path=root,
        template=[str(fake),'-p','{prompt}'],model=None,effort=None,
        agent='probe',timeout=600,scaffold=False,env=env)
    results.append(rc)
try:
    if mode in ('combined', 'stop'):
        # A live bridge can be between sessions. Queue both wakes before
        # allowing that bridge to enter its next session.
        original=r.read_live_pid
        r.read_live_pid=lambda home: os.getpid()
        for f in files: enqueue([f])
        r.read_live_pid=original
        if mode=='stop':
            # The stopped bridge is already gone, but its queue remains.
            r.stop_resident(root,'probe',grace_seconds=.5)
        else:
            env={k:v for k,v in original_env.items() if not k.startswith('A8S_TURN_')}
            bridge_proc=subprocess.Popen([sys.executable,r.__file__,'serve',str(home)],
                cwd=root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                start_new_session=True)
            bridge=bridge_proc.pid
            r._write_pid_files(home,bridge)
            bridge_proc.wait(timeout=10)
    else:
        enqueue(files[:1])
        bridge=r.read_live_pid(home)
        if mode=='second':
            wait(lambda:(root/'first-done').exists() and (r.read_state(home) or {}).get('turns')==0)
            enqueue(files[1:])
        wait(lambda:not (home/'pid').exists())
        _, status=os.waitpid(bridge,0)
        assert os.waitstatus_to_exitcode(status)==1
    print(json.dumps({'enqueue':results}))
finally:
    if bridge:
        r.stop_resident(root,'probe',grace_seconds=.5)
        try: os.killpg(bridge,signal.SIGKILL)
        except ProcessLookupError: pass
        if bridge_proc is not None: bridge_proc.wait(timeout=3)
        else:
            try: os.waitpid(bridge,0)
            except ChildProcessError: pass
'''


@pytest.mark.skipif(os.name != 'posix', reason='executable fake and bounded group cleanup')
@pytest.mark.parametrize('mode, failed', [('exit', [0]), ('combined', [0,1]), ('second', [1]), ('stop', [0,1])])
def test_real_enqueue_bridge_failure_then_local_consumer(node, tmp_path, mode, failed):
    files = [envelope(node, 'first'), envelope(node, 'second')]
    env = dict(os.environ)
    env.update(daemon._wake_env(node, {}, files))
    env.update(HOME=str(tmp_path/'home'), XDG_CONFIG_HOME=str(tmp_path/'config'))
    repo = Path(__file__).resolve().parents[3]
    result = subprocess.run([sys.executable, '-c', RUNNER, str(repo), str(node.root), mode],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout+result.stderr
    assert all(rc==0 for rc in json.loads(result.stdout)['enqueue'])
    for file in files:
        daemon._settle_wake(node, [file], 0)
    daemon._consume_wake_failures(node.name)
    assert [i for i,f in enumerate(files) if (dead_letters_dir(node.name)/f.name).exists()] == failed
    assert len(failed_events()) == len(failed)
    daemon._consume_wake_failures(node.name)
    assert len(failed_events()) == len(failed)


SPAWN_FAILURE_RUNNER = r'''
import os, sys, json
from pathlib import Path
repo, root, earlier = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path[:0] = [str(repo/'lib'), str(repo/'apps/r4t')]
from engines import resident as r
env = dict(os.environ)
def enqueue(wake_env):
    return r.enqueue_or_start('claude', 'message', dir_path=root,
        template=['unused-fake-engine', '-p', '{prompt}'], model=None,
        effort=None, agent='probe', timeout=60, scaffold=False, env=wake_env)
if earlier:
    # Earlier accepted idle and message wakes remain in the dead bridge's
    # inbox. Both need outcomes when the next enqueuer cannot start a bridge.
    live_pid = r.read_live_pid
    r.read_live_pid = lambda home: os.getpid()
    assert enqueue(dict(env, A8S_TURN_ENVELOPES='[]')) == 0
    assert enqueue(dict(env, A8S_TURN_ENVELOPES=json.dumps([earlier]))) == 0
    r.read_live_pid = live_pid
def fail(*args, **kwargs):
    raise OSError('simulated bridge spawn failure')
r.subprocess.Popen = fail
rc = enqueue(env)
assert not r._inbox_waiting(r.resident_home(root, 'probe'))
sys.exit(rc)
'''


@pytest.mark.parametrize('idle', [False, True])
@pytest.mark.parametrize('with_earlier', [False, True])
def test_bridge_spawn_failure_counts_current_wake_once(node, tmp_path, idle, with_earlier):
    current = envelope(node, 'current')
    earlier = envelope(node, 'earlier')
    files = [] if idle else [current]
    env = dict(os.environ)
    env.update(daemon._wake_env(node, {}, files))
    env.update(HOME=str(tmp_path/'home'), XDG_CONFIG_HOME=str(tmp_path/'config'))
    repo = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [sys.executable, '-c', SPAWN_FAILURE_RUNNER, str(repo), str(node.root),
         str(earlier) if with_earlier else ''],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    records = [json.loads(p.read_text())
               for p in outcome_dir(agent_dir(node.name)).glob('*.json')]
    assert sorted(r['envelopes'] for r in records) == (
        [[], [earlier.name]] if with_earlier else [])
    if with_earlier:
        daemon._settle_idle(node.name, 0)
        daemon._settle_wake(node, [earlier], 0)
    if idle:
        daemon._settle_idle(node.name, result.returncode)
    else:
        daemon._settle_wake(node, files, result.returncode)
    assert len(failed_events()) == 1
    daemon._consume_wake_failures(node.name)
    assert len(failed_events()) == (3 if with_earlier else 1)
    assert (dead_letters_dir(node.name)/current.name).exists() is (not idle)
    assert (dead_letters_dir(node.name)/earlier.name).exists() is with_earlier
    daemon._consume_wake_failures(node.name)
    assert len(failed_events()) == (3 if with_earlier else 1)


def test_stop_hook_consumes_new_failures_after_hook(node, monkeypatch):
    file = envelope(node, 'stop')
    context = origin(node, [file])
    monkeypatch.setattr(daemon, '_run_stop_hook',
                        lambda name: report_failure(context, 'stop'))
    daemon.run_stop_hook(node.name)
    assert (dead_letters_dir(node.name)/file.name).exists()
    assert len(failed_events()) == 1


def test_consumer_waits_for_another_process_wake_to_settle(node):
    from core import write_wake_pid, clear_wake_pid, pid_path
    file = envelope(node, 'concurrent')
    report_failure(origin(node, [file]), 'failed')
    # The helper stands for another handler, still alive while its wake has
    # exited but its settlement callback has not finished.
    helper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
    try:
        pid_path(node.name).write_text(str(helper.pid))
        daemon._stamp_pid_start(node.name, helper.pid)
        write_wake_pid(node.name, helper.pid, [file.name])
        daemon._consume_wake_failures(node.name)
        assert not failed_events()
        daemon._settle_wake(node, [file], 0)
        clear_wake_pid(node.name)
        daemon._consume_wake_failures(node.name)
        assert len(failed_events()) == 1
    finally:
        helper.terminate()
        helper.wait(timeout=3)


@pytest.mark.parametrize('handler', ['own', 'dead', 'absent'])
def test_stale_wake_marker_does_not_block_failure_after_restart(node, handler):
    from core import write_wake_pid, pid_path, wake_pid_path
    file = envelope(node, 'before-restart')
    report_failure(origin(node, [file]), 'detached engine failed')
    previous = subprocess.Popen([sys.executable, '-c', 'pass'])
    previous.wait(timeout=3)
    write_wake_pid(node.name, previous.pid, [file.name])
    if handler != 'absent':
        holder = os.getpid() if handler == 'own' else previous.pid
        pid_path(node.name).write_text(str(holder))
        daemon._stamp_pid_start(node.name, holder)
    assert wake_pid_path(node.name).exists()
    assert daemon._CURRENT_WAKE_NAME is None
    # No new message or enqueue: the first consume in the new handler (or
    # the stop helper after the old handler died) accounts for the failure.
    daemon._consume_wake_failures(node.name)
    assert (dead_letters_dir(node.name)/file.name).exists()
    assert read_last_wake(node.name) is False
    assert len(failed_events()) == 1


@pytest.mark.skipif(os.name != 'posix', reason='real a8s handler process')
def test_a8s_stop_consumes_hook_child_failure_before_return(tmp_path):
    from test_stop_hook import Rig
    import signal
    repo = Path(__file__).resolve().parents[3]
    hook = (
        f"import sys,os; sys.path.insert(0,{str(repo/'lib')!r}); "
        "from ar3.wake_outcome import origin_from_env,report_failure; "
        "report_failure(origin_from_env(dict(os.environ),'n1'),'interrupted resident turn')"
    )
    rig = Rig(tmp_path, stop_script=hook)
    pid = rig.start()
    try:
        stopped = rig.a8s('stop','n1')
        assert stopped.returncode == 0, stopped.stdout+stopped.stderr
        assert (rig.agent/'last-wake').read_text().strip() == 'failed'
        assert not list((rig.agent/'wake-outcomes').glob('*.json'))
        assert 'resident wake failed' in (rig.agent/'log.txt').read_text()
    finally:
        try: os.kill(pid,signal.SIGKILL)
        except ProcessLookupError: pass
