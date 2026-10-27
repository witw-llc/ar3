"""Real bridge/pipe shutdown with a bounded, local fake MSP host only."""
import json
import sys
import threading

import pytest

from ar3.wake_outcome import origin_from_env, take_failures
from engines import resident as r


@pytest.mark.parametrize('case', [
    'clean', 'unsolicited', 'active', 'other_reason', 'late_error', 'late_exit',
])
def test_muse_bridge_eof_host_shutdown(tmp_path, monkeypatch, case):
    fake = tmp_path / 'fake_msp.py'
    fake.write_text('''import json, os, sys, threading
threading.Timer(7, lambda: os._exit(90)).start()
case = sys.argv[1]
def emit(obj):
    print(json.dumps(obj), flush=True)
def reply(req, result):
    emit({'id': req['id'], 'result': result})
def note(method, **params):
    emit({'method': method, 'params': {'sessionId': 'session-a', **params}})
def close(reason='hostShutdown'):
    note('session/closed', reason=reason, viewCursor=None)
for line in sys.stdin:
    req = json.loads(line)
    method = req['method']
    if method == 'initialize':
        reply(req, {'schema': {'version': 1}, 'grantedCapabilities': ['sessionMcp'],
                    'experimentalApi': True})
    elif method == 'session/start':
        reply(req, {'session': {'sessionId': 'session-a',
              'approvalMode': {'mode': req['params']['approvalMode']},
              'permissionState': {'profileId': ':ask-me', 'reviewerAuthority': 'human',
                  'reviewerAttached': True, 'axes': {'filesystem': 'sandboxed',
                      'network': 'off', 'approval': 'onRequest', 'reviewer': 'human'}}}})
        if case == 'unsolicited':
            close()
    elif method == 'turn/start':
        command = req['params']['commandId']
        reply(req, {'commandId': command, 'status': 'accepted',
                   'disposition': 'started', 'turnId': command})
        note('turn/started', commandId=command, turnId=command)
        if case == 'active':
            close()
        else:
            note('turn/completed', turnId=command, terminal='completed')
close('idle' if case == 'other_reason' else 'hostShutdown')
if case == 'late_error':
    note('view/gap', after='cursor-a', next='cursor-c')
os._exit(7 if case == 'late_exit' else 0)
''', encoding='utf-8')
    home = r.resident_home(tmp_path, 'probe')
    origin = origin_from_env({
        'A8S_TURN_RECIPIENT': 'probe',
        'A8S_TURN_ENVELOPES': '["original.json"]',
        'A8S_TURN_OUTCOME_DIR': str(tmp_path / 'failures'),
    }, 'probe')
    r._enqueue_message(home, 'hello', origin)
    monkeypatch.setattr(r, 'compose_resident_argv', lambda template, *a, **kw: template)
    spawned = []
    real_popen = r.subprocess.Popen
    def spawn(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc
    monkeypatch.setattr(r.subprocess, 'Popen', spawn)
    outcome = r._run_session(home, {
        'engine': 'muse', 'dir_path': str(tmp_path), 'agent': 'probe',
        'template': [sys.executable, str(fake), case], 'timeout': 1, 'scaffold': False,
    }, threading.Event(), {})
    assert len(spawned) == 1 and spawned[0].poll() is not None
    records = list(take_failures(tmp_path / 'failures', 'probe'))
    if case == 'clean':
        assert outcome == 0 and spawned[0].returncode == 0
        assert records == []
    else:
        assert outcome == 1
        assert len(records) == 1
        assert records[0]['envelopes'] == (
            ['original.json'] if case in ('active', 'unsolicited') else [])
