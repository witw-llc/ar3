"""Generic ACP session behavior. Cursor-only deltas stay in the cursor tests."""
from __future__ import annotations

import json
from pathlib import Path

from engines.acp import AcpSession, permission_outcome


class Plain(AcpSession):
    def auth_method(self) -> str:
        return "login"


def _ready(tmp_path: Path) -> Plain:
    sess = Plain(["tool", "acp"], tmp_path)
    init = sess.on_frame({"id": 1, "result": {
        "protocolVersion": 1,
        "authMethods": [{"id": "login"}],
    }})
    assert init.fatal is False
    assert json.loads(init.replies[0])["method"] == "authenticate"
    auth = sess.on_frame({"id": 2, "result": {}})
    assert json.loads(auth.replies[0])["method"] == "session/new"
    opened = sess.on_frame({"id": 3, "result": {"sessionId": "s"}})
    assert opened.fatal is False
    assert sess.ready
    return sess


class TestAcpSession:
    def test_a_wrong_protocol_version_is_fatal(self, tmp_path):
        sess = Plain(["tool", "acp"], tmp_path)
        parsed = sess.on_frame({"id": 1, "result": {"protocolVersion": 999}})
        assert parsed.fatal
        assert "999" in parsed.error_text

    def test_opening_is_initialize_only(self, tmp_path):
        sess = Plain(["tool", "acp"], tmp_path)
        (line,) = sess.opening_lines()
        assert json.loads(line)["method"] == "initialize"

    def test_an_unoffered_auth_method_fails_closed(self, tmp_path):
        sess = Plain(["tool", "acp"], tmp_path)
        parsed = sess.on_frame({"id": 1, "result": {
            "protocolVersion": 1,
            "authMethods": [{"id": "other"}],
        }})
        assert parsed.fatal
        assert "login" in parsed.error_text
        assert not sess.ready

    def test_no_auth_method_skips_authenticate(self, tmp_path):
        sess = AcpSession(["tool", "acp"], tmp_path)
        parsed = sess.on_frame({"id": 1, "result": {"protocolVersion": 1}})
        assert parsed.fatal is False
        assert json.loads(parsed.replies[0])["method"] == "session/new"
        assert not sess.ready

    def test_session_new_before_the_handshake_replies_is_not_ready(self, tmp_path):
        sess = Plain(["tool", "acp"], tmp_path)
        parsed = sess.on_frame({"id": 3, "result": {"sessionId": "s"}})
        assert parsed.fatal is False
        assert not sess.ready
        init = sess.on_frame({"id": 1, "result": {
            "protocolVersion": 1,
            "authMethods": [{"id": "login"}],
        }})
        assert not sess.ready
        assert json.loads(init.replies[0])["method"] == "authenticate"
        auth = sess.on_frame({"id": 2, "result": {}})
        assert auth.replies == ()
        assert sess.ready

    def test_a_permission_for_another_session_is_fatal(self, tmp_path):
        sess = _ready(tmp_path)
        parsed = sess.on_frame({
            "id": 0,
            "method": "session/request_permission",
            "params": {
                "sessionId": "other",
                "options": [{"optionId": "reject-once", "kind": "reject_once"}],
            },
        })
        assert parsed.fatal
        assert sess.session_id == "s"

    def test_a_permission_keeps_id_zero(self, tmp_path):
        sess = _ready(tmp_path)
        parsed = sess.on_frame({
            "id": 0,
            "method": "session/request_permission",
            "params": {
                "sessionId": "s",
                "options": [{"optionId": "reject-once", "kind": "reject_once"}],
            },
        })
        assert parsed.fatal is False
        reply = json.loads(parsed.replies[0])
        assert reply["id"] == 0
        assert reply["result"]["outcome"]["optionId"] == "reject-once"

    def test_an_unknown_request_is_refused(self, tmp_path):
        sess = _ready(tmp_path)
        parsed = sess.on_frame({"id": 7, "method": "session/unknown"})
        reply = json.loads(parsed.replies[0])
        assert reply["id"] == 7
        assert reply["error"]["code"] == -32601

    def test_a_second_session_new_is_ignored(self, tmp_path):
        sess = _ready(tmp_path)
        parsed = sess.on_frame({"id": 3, "result": {"sessionId": "other"}})
        assert parsed.fatal is False
        assert parsed.replies == ()
        assert sess.session_id == "s"
        assert sess.ready

    def test_a_malformed_update_fails_the_session(self, tmp_path):
        sess = _ready(tmp_path)
        parsed = sess.on_frame({"method": "session/update", "params": ["nope"]})
        assert parsed.fatal
        assert sess.error

    def test_a_different_session_id_fails_the_session(self, tmp_path):
        sess = _ready(tmp_path)
        parsed = sess.on_frame({
            "method": "session/update",
            "params": {"sessionId": "other", "update": {}},
        })
        assert parsed.fatal

    def test_chunks_assemble_into_one_text_on_the_turn(self, tmp_path):
        sess = _ready(tmp_path)
        (line,) = sess.turn_lines("hello")
        for piece in ("Hel", "lo"):
            chunk = sess.on_frame({
                "method": "session/update",
                "params": {
                    "sessionId": "s",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": piece},
                    },
                },
            })
            assert chunk.assistant_text == ""
            assert chunk.turn_completed is False
        done = sess.on_frame({
            "id": json.loads(line)["id"],
            "result": {"stopReason": "end_turn"},
        })
        assert done.assistant_text == "Hello"
        assert done.result_text == "Hello"
        assert done.turn_completed

    def test_a_missing_permission_kind_cancels(self):
        result = permission_outcome(
            [{"optionId": "allow-always", "kind": "allow_always"}],
            allow=True,
        )
        assert result == {"outcome": {"outcome": "cancelled"}}

    def test_handshake_deadline_fires_until_ready(self, tmp_path):
        sess = Plain(["tool", "acp"], tmp_path)
        sess.started = 0
        assert sess.deadline_error()
        _ready(tmp_path)
        ready = Plain(["tool", "acp"], tmp_path)
        ready.ready = True
        ready.started = 0
        assert ready.deadline_error() is None

    def test_an_error_does_not_disarm_the_deadline_before_the_handshake(self, tmp_path):
        sess = Plain(["tool", "acp"], tmp_path)
        sess.started = 0
        sess.error = "acp session/update params were not an object"
        assert sess.deadline_error()
        sess.handshake_done = True
        assert sess.deadline_error() is None

    def test_every_rpc_line_is_jsonrpc_2(self, tmp_path):
        sess = Plain(["tool", "acp"], tmp_path)
        line = json.loads(sess.opening_lines()[0])
        assert line["jsonrpc"] == "2.0"
        assert line["method"] == "initialize"

    def test_a_session_new_failure_names_its_message(self, tmp_path):
        sess = Plain(["tool", "acp"], tmp_path)
        sess.on_frame({"id": 1, "result": {
            "protocolVersion": 1,
            "authMethods": [{"id": "login"}],
        }})
        sess.on_frame({"id": 2, "result": {}})
        parsed = sess.on_frame({"id": 3, "error": {"code": -1, "message": "no room"}})
        assert parsed.fatal
        assert parsed.error_text == "acp session/new failed: no room"
        assert not sess.ready

    def test_a_failed_session_stays_failed(self, tmp_path):
        sess = Plain(["tool", "acp"], tmp_path)
        first = sess.on_frame({"id": 1, "result": {
            "protocolVersion": 1,
            "authMethods": [{"id": "other"}],
        }})
        assert first.fatal
        assert not sess.ready
        second = sess.on_frame({"id": 2, "result": {}})
        assert second.fatal
        assert second.error_text == first.error_text
        third = sess.on_frame({"id": 3, "result": {"sessionId": "s"}})
        assert third.fatal
        assert not sess.ready
        assert sess.turn_lines("hi") == []
