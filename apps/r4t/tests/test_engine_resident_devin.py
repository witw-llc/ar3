from __future__ import annotations

import copy
import json
import os
import sys
import threading

import pytest

from engines import devin_resident as devin
from engines import resident, run


def catalog():
    return {"sessionId": "test-session", "configOptions": [
        {"id": "mode", "type": "select", "currentValue": "bypass",
         "options": [{"value": v} for v in ("accept-edits", "smart", "ask", "bypass")]},
        {"id": "model", "type": "select", "currentValue": "model-a",
         "options": [{"value": v} for v in ("model-a", "model-b")]},
        {"id": "thought_level", "type": "select", "currentValue": "high",
         "options": [{"value": v} for v in ("medium", "high", "max")]},
    ]}


def session(tmp_path, mode="accept-edits", model=None, effort=None):
    env = {"R4T_SESSION_MODE": mode}
    if model:
        env["R4T_SESSION_MODEL"] = model
    if effort:
        env["R4T_SESSION_EFFORT"] = effort
    obj = devin.DevinSession(["devin", "acp"], tmp_path, env)
    assert json.loads(obj.opening_lines()[0])["method"] == "initialize"
    parsed = obj.on_frame({"id": 1, "result": {
        "protocolVersion": 1, "authMethods": [{"id": "devin-browser"}],
    }})
    new = json.loads(parsed.replies[0])
    assert new["method"] == "session/new"
    assert new["params"]["mcpServers"][0]["name"] == "a8s"
    assert isinstance(new["params"]["mcpServers"][0]["env"], list)
    return obj


def settle(obj, result=None, *, wrong=False):
    result = copy.deepcopy(result or catalog())
    parsed = obj.on_frame({"id": 3, "result": result})
    requests = []
    while parsed.replies and not obj.ready:
        req = json.loads(parsed.replies[0])
        assert req["method"] == "session/set_config_option"
        requests.append(req)
        pin = req["params"]
        for opt in result["configOptions"]:
            if opt["id"] == pin["configId"] and not wrong:
                opt["currentValue"] = pin["value"]
        parsed = obj.on_frame({"id": req["id"], "result": result})
    return parsed, requests


@pytest.mark.parametrize("permission,mode", [(None,"accept-edits"),("ask","ask"),("auto","smart"),("bypass","bypass")])
def test_composition_preserves_one_shot_and_explicitly_pins_policy(tmp_path, permission, mode):
    argv, _ = run._build_argv_template("devin", workdir=tmp_path, timeout=30,
                                     resident=True, permissions=permission,
                                     model="model-a", effort="high")
    env = {"R4T_SESSION_MODEL": "stale"}
    assert resident.compose_resident_argv(argv, env, engine="devin") == ["devin", "acp"]
    assert env["R4T_SESSION_MODE"] == mode
    assert env["R4T_SESSION_MODEL"] == "model-a"
    assert env["R4T_SESSION_EFFORT"] == "high"
    assert "dangerous" not in argv
    if permission in ("ask", "auto"):
        with pytest.raises(run.RunError, match="headless"):
            run._build_argv_template("devin", workdir=tmp_path, timeout=30, model=None, permissions=permission)


def test_prompts_wait_for_all_effective_pins(tmp_path):
    obj = session(tmp_path, model="model-b", effort="max")
    assert obj.turn_lines("one") == []
    assert obj.turn_lines("two") == []
    parsed, requests = settle(obj)
    assert obj.ready
    assert [r["params"]["configId"] for r in requests] == ["mode", "model", "thought_level"]
    prompt = json.loads(parsed.replies[0])
    assert prompt["method"] == "session/prompt"
    assert prompt["params"]["prompt"][0]["text"] == "one"
    parsed = obj.on_frame({"id": prompt["id"], "result": {"stopReason": "end_turn"}})
    assert json.loads(parsed.replies[0])["params"]["prompt"][0]["text"] == "two"


@pytest.mark.parametrize("model,effort", [("unknown",None),(None,"unknown")])
def test_unknown_config_value_fails_before_prompt(tmp_path, model, effort):
    obj = session(tmp_path, model=model, effort=effort)
    obj.turn_lines("held")
    parsed, _ = settle(obj)
    assert parsed.fatal
    assert "catalog" in parsed.error_text
    assert not obj.ready
    assert not obj.turn_requests


def test_effective_policy_mismatch_fails_closed(tmp_path):
    obj = session(tmp_path)
    obj.turn_lines("held")
    parsed, _ = settle(obj, wrong=True)
    assert parsed.fatal
    assert "not confirmed" in parsed.error_text
    assert not obj.turn_requests


def test_missing_catalog_and_rejected_pin_fail_closed(tmp_path):
    obj = session(tmp_path)
    assert obj.on_frame({"id": 3, "result": {"sessionId": "test-session"}}).fatal
    obj = session(tmp_path)
    parsed = obj.on_frame({"id": 3, "result": catalog()})
    rid = json.loads(parsed.replies[0])["id"]
    assert obj.on_frame({"id": rid, "error": {"message": "not supported"}}).fatal


@pytest.mark.parametrize("mode,expected", [("accept-edits","deny"),("smart","deny"),("ask","deny"),("bypass","allow")])
def test_permissions_only_allow_once_in_explicit_bypass(tmp_path, mode, expected):
    obj = session(tmp_path, mode=mode)
    settle(obj)
    parsed = obj.on_frame({"id": "permission", "method": "session/request_permission",
                          "params": {"sessionId": "test-session", "options": [
                              {"kind": "allow_always", "optionId": "persistent"},
                              {"kind": "allow_once", "optionId": "allow"},
                              {"kind": "reject_once", "optionId": "deny"},
                          ]}})
    assert json.loads(parsed.replies[0])["result"]["outcome"]["optionId"] == expected


@pytest.mark.parametrize("bad_pin", [False, True])
def test_real_fake_devin_handshake_and_serial_turns(monkeypatch, tmp_path, bad_pin):
    record = tmp_path / "wire.jsonl"
    marker = tmp_path / "prompt-ready"
    fake = tmp_path / "fake.py"
    fake.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        f"result={catalog()!r}\n"
        "for line in sys.stdin:\n"
        " req=json.loads(line)\n"
        f" with open({str(record)!r},'a',encoding='utf-8',newline='') as out: out.write(line)\n"
        " method=req['method']; params=req.get('params',{})\n"
        " if method=='initialize': answer={'protocolVersion':1,'authMethods':[{'id':'devin-browser'}]}\n"
        " elif method=='session/new': answer=result\n"
        " elif method=='session/set_config_option':\n"
        "  for opt in result['configOptions']:\n"
        f"   if opt['id']==params['configId'] and not {bad_pin!r}: opt['currentValue']=params['value']\n"
        "  answer=result\n"
        " elif method=='session/prompt':\n"
        f"  Path({str(marker)!r}).write_text('ready'); answer={{'stopReason':'end_turn'}}\n"
        " else: raise RuntimeError(method)\n"
        " print(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':answer}),flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(run, "resolve_argv0", lambda argv: [sys.executable, str(fake)])
    monkeypatch.setattr(resident, "POLL_SECONDS", .01)
    home = tmp_path / "resident"
    (home / "inbox").mkdir(parents=True)
    resident._enqueue_message(home, "first")
    spec = {"engine":"devin", "dir_path":str(tmp_path), "agent":"test-seat",
            "template":devin.build_template(None,None,None), "timeout":1,
            "scaffold":False, "memory":"off"}
    stop = threading.Event()
    watchdog = threading.Timer(6, stop.set)
    watchdog.daemon = True
    watchdog.start()
    def enqueue_second():
        while not marker.exists() and not stop.wait(.01):
            pass
        if not stop.is_set():
            resident._enqueue_message(home, "second")
    sender = None
    if not bad_pin:
        sender = threading.Thread(target=enqueue_second, daemon=True)
        sender.start()
    try:
        code = resident._run_session(home, spec, stop, {})
    finally:
        watchdog.cancel()
    assert not stop.is_set()
    if sender:
        sender.join(timeout=1)
        assert not sender.is_alive()
    wire = [json.loads(x) for x in record.read_text().splitlines()]
    methods = [x['method'] for x in wire]
    assert 'authenticate' not in methods
    assert methods.count('session/new') == 1
    prompts = [x for x in wire if x['method']=='session/prompt']
    if bad_pin:
        assert code != 0
        assert not prompts
    else:
        assert code == 0
        assert len(prompts) >= 2
        assert methods.index('session/set_config_option') < methods.index('session/prompt')


def test_definition_hooks_are_resident_and_policy_is_explicit():
    from pathlib import Path

    defs = Path(__file__).resolve().parents[2] / "a8s" / "definitions"
    for name, bypass in (("engine-devin-resident", False), ("engine-devin-resident-unrestricted", True)):
        d = json.loads((defs / (name + ".json")).read_text())
        for argv in (d['invoke'], d['batch']['invoke'], d['idle']['invoke']):
            assert '--resident' in argv
            assert ('--permissions=bypass' in argv) == bypass
        assert 'stop' in d['stop']['invoke']
        assert 'status' in d['status']['invoke']


@pytest.mark.parametrize("problem", [None, "devin mode pin was not confirmed"])
def test_check_uses_no_turn_effective_config_probe(monkeypatch, tmp_path, problem):
    from engines import check

    from conftest import write_path_executable

    calls = []
    fake = write_path_executable(tmp_path / "bin", "devin", "pass\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(check, "interpreter_gap", lambda *a, **k: None)
    monkeypatch.setattr(check, "_version", lambda launch: devin.PROVEN_VERSION)
    monkeypatch.setattr(check, "_run", lambda *args, **kwargs: (0, "help"))
    def probe(argv, cwd, *, env):
        calls.append((argv, env))
        return {"error": problem} if problem else {"model": "model-a"}
    monkeypatch.setattr(resident, "devin_acp_probe", probe)
    report = check.check_engine("devin", resident=True, workdir=tmp_path,
                               model="model-a", effort="high", permissions="ask")
    assert report.verdict == (check.REJECTED if problem else check.ACCEPTED)
    assert report.method == "acp handshake"
    assert calls[0][0] == [str(fake), "acp"]
    assert calls[0][1]["R4T_SESSION_MODE"] == "ask"


def test_effort_catalog_is_read_after_the_model_pin(tmp_path):
    obj = session(tmp_path, model="model-b", effort="max")
    result = catalog()
    parsed = obj.on_frame({"id": 3, "result": result})
    req = json.loads(parsed.replies[0])
    result['configOptions'][0]['currentValue'] = 'accept-edits'
    parsed = obj.on_frame({'id': req['id'], 'result': result})
    req = json.loads(parsed.replies[0])
    assert req['params']['configId'] == 'model'
    result['configOptions'][1]['currentValue'] = 'model-b'
    result['configOptions'][2]['options'] = [{'value':'high'}]
    parsed = obj.on_frame({'id': req['id'], 'result': result})
    assert parsed.fatal
    assert 'thought_level' in parsed.error_text
    assert not obj.turn_requests


def test_terminal_prompt_error_does_not_release_queued_turn(tmp_path):
    obj = session(tmp_path)
    settle(obj)
    first = json.loads(obj.turn_lines('first')[0])
    assert obj.turn_lines('second') == []
    parsed = obj.on_frame({'id':first['id'], 'error':{'message':'not authenticated'}})
    assert parsed.turn_failed
    assert parsed.fatal
    assert parsed.turn_completed
    assert not parsed.replies


def test_long_held_prompt_is_redated_and_noted_once(tmp_path, monkeypatch):
    from engines import held_input

    obj = session(tmp_path)
    settle(obj)
    first = json.loads(obj.turn_lines('first')[0])
    order = '[t] alice tells bob (4 seconds ago): ship'
    assert obj.turn_lines(held_input.QueuedText.from_items(
        [{'message': order, 'queued_at': 0.0}])) == []
    monkeypatch.setattr(held_input, '_now', lambda: 5 * 60.0)
    parsed = obj.on_frame({'id': first['id'], 'result': {'stopReason': 'end_turn'}})
    (line,) = parsed.replies
    sent = json.loads(line)['params']['prompt'][0]['text']
    assert sent.startswith('[This input was held 5 minutes')
    assert sent.endswith('[t] alice tells bob (5 minutes ago): ship')
    assert not obj.held


@pytest.fixture
def native_tell_permission():
    from pathlib import Path

    path = Path(__file__).parent / 'fixtures' / 'devin-acp' / 'tell-permission.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def active_session(tmp_path, mode='accept-edits'):
    obj = session(tmp_path, mode=mode)
    settle(obj)
    prompt = json.loads(obj.turn_lines('one local tell')[0])
    return obj, prompt


def permission_choice(parsed):
    result = json.loads(parsed.replies[0])['result']['outcome']
    return result.get('optionId', result['outcome'])


def set_call_id(frames, call_id):
    frames[0]['params']['update']['toolCallId'] = call_id
    frames[1]['params']['toolCall']['toolCallId'] = call_id


def test_native_exact_tell_identity_allows_once_and_cannot_replay(tmp_path, native_tell_permission):
    obj, _ = active_session(tmp_path)
    update, request = native_tell_permission
    obj.on_frame(update)
    assert permission_choice(obj.on_frame(request)) == 'allow_once'
    assert obj.mode == 'accept-edits'
    assert permission_choice(obj.on_frame(request)) == 'reject_once'
    obj.on_frame(update)
    assert permission_choice(obj.on_frame(request)) == 'reject_once'


@pytest.mark.parametrize('mode', ['ask', 'smart'])
def test_explicit_modes_keep_rejecting_even_exact_tell(tmp_path, native_tell_permission, mode):
    obj, _ = active_session(tmp_path, mode)
    obj.on_frame(native_tell_permission[0])
    assert permission_choice(obj.on_frame(native_tell_permission[1])) == 'reject_once'


@pytest.mark.parametrize('meta', [
    None, {}, [],
    {'cognition.ai/toolName': 'mcp__a8s__tell'},
    {**devin.TELL_IDENTITY, 'cognition.ai/toolName': 'mcp__other__tell'},
    {**devin.TELL_IDENTITY, 'cognition.ai/toolName': 'mcp__a8s__other'},
    {**devin.TELL_IDENTITY, 'cognition.ai/eventType': 'exec'},
    {**devin.TELL_IDENTITY, 'cognition.ai/inferenceToolName': 'exec'},
])
def test_missing_or_conflicting_native_identity_never_grants(tmp_path, native_tell_permission, meta):
    obj, _ = active_session(tmp_path)
    update, request = native_tell_permission
    update['params']['update']['_meta'] = meta
    update['params']['update']['title'] = 'mcp__a8s__tell'
    update['params']['update']['name'] = 'mcp__a8s__tell'
    update['params']['update']['rawInput'] = {'tool': 'mcp__a8s__tell'}
    obj.on_frame(update)
    assert permission_choice(obj.on_frame(request)) == 'reject_once'


def test_permission_before_native_update_is_retired(tmp_path, native_tell_permission):
    obj, _ = active_session(tmp_path)
    update, request = native_tell_permission
    assert permission_choice(obj.on_frame(request)) == 'reject_once'
    obj.on_frame(update)
    assert permission_choice(obj.on_frame(request)) == 'reject_once'


@pytest.mark.parametrize('state', ['idle', 'before_ready'])
def test_native_identity_seen_outside_turn_cannot_be_replayed(tmp_path, native_tell_permission, state):
    obj = session(tmp_path)
    if state == 'idle':
        settle(obj)
    update, request = native_tell_permission
    obj.on_frame(update)
    assert obj.tell_calls[update['params']['update']['toolCallId']] is None
    if state == 'before_ready':
        settle(obj)
    obj.turn_lines('fresh turn')
    obj.on_frame(update)
    assert permission_choice(obj.on_frame(request)) == 'reject_once'


@pytest.mark.parametrize('status', ['completed', 'failed', 'unknown'])
def test_terminal_permission_request_consumes_native_identity(tmp_path, native_tell_permission, status):
    obj, _ = active_session(tmp_path)
    update, request = native_tell_permission
    obj.on_frame(update)
    request['params']['toolCall']['status'] = status
    assert permission_choice(obj.on_frame(request)) == 'reject_once'
    request['params']['toolCall'].pop('status')
    assert permission_choice(obj.on_frame(request)) == 'reject_once'
    obj.on_frame(update)
    assert permission_choice(obj.on_frame(request)) == 'reject_once'


@pytest.mark.parametrize('change', ['duplicate', 'conflict', 'request_conflict', 'completed', 'failed'])
def test_changed_duplicate_or_terminal_identity_cannot_regrant(tmp_path, native_tell_permission, change):
    obj, _ = active_session(tmp_path)
    update, request = native_tell_permission
    obj.on_frame(update)
    changed = copy.deepcopy(update)
    if change == 'request_conflict':
        request['params']['toolCall']['_meta'] = {'cognition.ai/toolName': 'exec'}
    else:
        if change != 'duplicate':
            changed['params']['update']['sessionUpdate'] = 'tool_call_update'
        if change == 'conflict':
            changed['params']['update']['_meta']['cognition.ai/toolName'] = 'exec'
        elif change in ('completed', 'failed'):
            changed['params']['update']['status'] = change
        obj.on_frame(changed)
    assert permission_choice(obj.on_frame(request)) == 'reject_once'
    obj.on_frame(update)
    request['params']['toolCall'].pop('_meta', None)
    assert permission_choice(obj.on_frame(request)) == 'reject_once'


def test_native_partial_in_progress_update_preserves_exact_identity(tmp_path, native_tell_permission):
    obj, _ = active_session(tmp_path)
    update, request = native_tell_permission
    obj.on_frame(update)
    partial = copy.deepcopy(update)
    partial['params']['update'] = {
        'sessionUpdate': 'tool_call_update',
        'toolCallId': request['params']['toolCall']['toolCallId'],
        'status': 'in_progress',
        '_meta': {'cognition.ai/inferenceToolName': 'mcp__a8s__tell'},
    }
    obj.on_frame(partial)
    assert permission_choice(obj.on_frame(request)) == 'allow_once'


@pytest.mark.parametrize('which', [0, 1])
def test_permission_identity_requires_own_session(tmp_path, native_tell_permission, which):
    obj, _ = active_session(tmp_path)
    obj.on_frame(native_tell_permission[0])
    native_tell_permission[which]['params']['sessionId'] = 'other-session'
    assert obj.on_frame(native_tell_permission[which]).fatal
    assert obj.tell_turn is None
    assert all(value is None for value in obj.tell_calls.values())


@pytest.mark.parametrize('inactive', ['idle', 'not_ready', 'cancelled'])
def test_permission_requires_active_verified_uncancelled_turn(tmp_path, native_tell_permission, inactive):
    obj, prompt = active_session(tmp_path)
    obj.on_frame(native_tell_permission[0])
    if inactive == 'idle':
        obj.on_frame({'id': prompt['id'], 'result': {'stopReason': 'end_turn'}})
    elif inactive == 'not_ready':
        obj.ready = False
    else:
        assert obj.cancel_line()
        assert obj.tell_turn is None
    assert permission_choice(obj.on_frame(native_tell_permission[1])) == 'reject_once'


def test_prompt_boundary_retires_old_ids_but_allows_fresh_ids(tmp_path, native_tell_permission):
    obj, prompt = active_session(tmp_path)
    update, request = native_tell_permission
    obj.on_frame(update)
    assert obj.turn_lines('next turn') == []
    done = obj.on_frame({'id': prompt['id'], 'result': {'stopReason': 'end_turn'}})
    assert done.turn_completed and done.replies
    assert permission_choice(obj.on_frame(request)) == 'reject_once'
    obj.on_frame(update)
    assert permission_choice(obj.on_frame(request)) == 'reject_once'
    fresh = copy.deepcopy(native_tell_permission)
    set_call_id(fresh, 'fresh-next-turn-call')
    obj.on_frame(fresh[0])
    assert permission_choice(obj.on_frame(fresh[1])) == 'allow_once'


def test_terminal_prompt_failure_retires_permission_identity(tmp_path, native_tell_permission):
    obj, prompt = active_session(tmp_path)
    obj.on_frame(native_tell_permission[0])
    assert obj.on_frame({'id': prompt['id'], 'error': {'message': 'failed'}}).fatal
    assert obj.tell_turn is None
    assert all(value is None for value in obj.tell_calls.values())


def test_server_session_or_bypass_options_are_never_selected(tmp_path, native_tell_permission):
    obj, _ = active_session(tmp_path)
    obj.on_frame(native_tell_permission[0])
    request = native_tell_permission[1]
    request['params']['options'] = [x for x in request['params']['options'] if x['kind'] == 'allow_always']
    assert permission_choice(obj.on_frame(request)) == 'cancelled'
    request['params']['options'] = [{'kind': 'allow_once', 'optionId': 'once'},
                                     {'kind': 'reject_once', 'optionId': 'deny'}]
    assert permission_choice(obj.on_frame(request)) == 'deny'


@pytest.mark.parametrize('call_id', [None, '', [], {}, 'x' * 513])
def test_invalid_call_ids_do_not_authorize_or_grow_state(tmp_path, native_tell_permission, call_id):
    obj, _ = active_session(tmp_path)
    set_call_id(native_tell_permission, call_id)
    obj.on_frame(native_tell_permission[0])
    assert permission_choice(obj.on_frame(native_tell_permission[1])) == 'reject_once'
    assert not obj.tell_calls


def test_identity_limit_fails_closed_without_evicting_replay_protection(tmp_path, monkeypatch, native_tell_permission):
    monkeypatch.setattr(devin, 'MAX_PERMISSION_CALLS', 2)
    obj, prompt = active_session(tmp_path)
    for call_id in ['one', 'two', 'three']:
        frames = copy.deepcopy(native_tell_permission)
        set_call_id(frames, call_id)
        obj.on_frame(frames[0])
    assert obj.tell_saturated and len(obj.tell_calls) <= 2
    obj.on_frame(native_tell_permission[0])
    assert permission_choice(obj.on_frame(native_tell_permission[1])) == 'reject_once'
    obj.on_frame({'id': prompt['id'], 'result': {'stopReason': 'end_turn'}})
    obj.turn_lines('next turn')
    set_call_id(native_tell_permission, 'new-turn')
    obj.on_frame(native_tell_permission[0])
    assert permission_choice(obj.on_frame(native_tell_permission[1])) == 'reject_once'
