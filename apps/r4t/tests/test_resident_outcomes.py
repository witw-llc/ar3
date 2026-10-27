from __future__ import annotations

import io
import json
import os
from pathlib import Path
import threading

from ar3.wake_outcome import origin_from_env, take_failures
from engines import resident as r


def context(tmp_path, agent='probe'):
    return origin_from_env({
        'A8S_TURN_RECIPIENT': agent,
        'A8S_TURN_ENVELOPES': json.dumps([str(tmp_path/'original.json')]),
        'A8S_TURN_OUTCOME_DIR': str(tmp_path/'failures'),
    }, agent)


def test_bridge_spawn_failure_drops_queue_without_replay(tmp_path, monkeypatch):
    def fail(*args, **kw):
        raise OSError('bridge cannot spawn')
    monkeypatch.setattr(r.subprocess, 'Popen', fail)
    env={**os.environ, 'A8S_TURN_RECIPIENT':'probe',
         'A8S_TURN_ENVELOPES':json.dumps([str(tmp_path/'original.json')]),
         'A8S_TURN_OUTCOME_DIR':str(tmp_path/'failures')}
    assert r.enqueue_or_start('claude','message',dir_path=tmp_path,
        template=['missing','-p','{prompt}'],model=None,effort=None,
        agent='probe',timeout=600,scaffold=False,env=env)==1
    assert not r._inbox_waiting(r.resident_home(tmp_path,'probe'))
    events=list(take_failures(tmp_path/'failures','probe'))
    # Its parent accounts for this synchronous failure from return code 1.
    assert events == []


def test_engine_exit_between_drain_and_send_retains_original_envelopes(tmp_path, monkeypatch):
    class ExitsBeforeSend:
        pid=os.getpid()
        stdin=io.StringIO()
        stdout=io.StringIO()
        returncode=0
        calls=0
        def poll(self):
            self.calls+=1
            return None if self.calls==1 else 0
    proc=ExitsBeforeSend()
    monkeypatch.setattr(r.subprocess,'Popen',lambda *a,**k:proc)
    monkeypatch.setattr(r,'stop_engine',lambda *a,**k:None)
    home=r.resident_home(tmp_path,'probe')
    r._enqueue_message(home,'hello',context(tmp_path))
    result=r._run_session(home,{'engine':'claude','dir_path':str(tmp_path),
        'template':[os.sys.executable,'-p','{prompt}'],'agent':'probe','scaffold':False},
        threading.Event(),{'proc':None})
    assert result==1
    events=list(take_failures(tmp_path/'failures','probe'))
    assert len(events)==1
    assert events[0]['envelopes']==['original.json']


def test_origin_matching_is_exact_despite_safe_key_collision(tmp_path):
    home=r.resident_home(tmp_path,'node.x')
    other=context(tmp_path,'node_x')
    assert r._item_origins([{'origin':other}],home,'node.x')==[]
