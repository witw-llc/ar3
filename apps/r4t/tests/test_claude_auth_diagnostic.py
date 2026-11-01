"""A live Claude authentication failure cannot leave a wake pending."""
import json
import sys
import threading

import pytest

from ar3.wake_outcome import origin_from_env, take_failures
from engines import resident


@pytest.mark.parametrize("engine,kind,failed", [
    ("claude", "auth", True),
    ("ollama-claude", "auth", True),
    ("claude", "after_success", True),
    ("claude", "warning", False),
    ("claude", "quoted", False),
    ("agy", "auth", False),
])
def test_live_diagnostic_is_engine_specific_and_preserves_outcomes(tmp_path, engine, kind, failed):
    diagnostic = "Failed to authenticate: OAuth session expired and could not be refreshed"
    child = tmp_path / "fake_vendor.py"
    child.write_text(
        f"engine={engine!r}\nkind={kind!r}\nfailed={failed!r}\ndiagnostic={diagnostic!r}\n"
        + """import json,sys
sent = json.loads(sys.stdin.readline())
result = ({'event': 'result', 'result': {'response': 'done'}}
          if engine == 'agy' else {'type': 'result', 'result': 'done'})
if sent.get('uuid'):
    # As Claude does: the result names the stdin message it answered.
    result['user_message_uuids'] = [sent['uuid']]
if kind == 'after_success':
    print(json.dumps(result), flush=True)
if kind == 'quoted':
    print(json.dumps({'type': 'assistant', 'message': {'content': diagnostic}}), flush=True)
else:
    print('warning: harmless diagnostic' if kind == 'warning' else diagnostic,
          file=sys.stderr, flush=True)
if not failed:
    print(json.dumps(result), flush=True)
for line in sys.stdin:
    pass
""", encoding="utf-8",
    )
    home = resident.resident_home(tmp_path, "probe")
    origin = origin_from_env({
        "A8S_TURN_RECIPIENT": "probe",
        "A8S_TURN_ENVELOPES": json.dumps([str(tmp_path / "message.json")]),
        "A8S_TURN_OUTCOME_DIR": str(tmp_path / "failures"),
    }, "probe")
    resident._enqueue_message(home, "fake local turn", origin)
    stop = threading.Event()
    rescue = threading.Timer(3, stop.set)
    rescue.start()
    try:
        code = resident._run_session(home, {
            "engine": engine, "dir_path": str(tmp_path), "agent": "probe",
            "template": [sys.executable, str(child), "--", "-p", "{prompt}"],
            "timeout": 0.05, "scaffold": False, "memory": "off",
        }, stop, {"proc": None})
    finally:
        rescue.cancel()
    assert not stop.is_set(), "live diagnostic required watchdog rescue"
    assert code == int(failed)
    events = list(take_failures(tmp_path / "failures", "probe"))
    assert len(events) == int(failed)
    if failed:
        assert events[0]["envelopes"] == ([] if kind == "after_success" else ["message.json"])
        assert events[0]["reason"] == diagnostic
    assert list(take_failures(tmp_path / "failures", "probe")) == []
