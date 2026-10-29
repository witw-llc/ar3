"""Delegated completion receipts authorize bounded reconciliation, not consumption."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from engines import resident as resident_mod


@pytest.fixture
def session(tmp_path):
    value = resident_mod.CodexSession(["codex", "app-server"], tmp_path)
    value.on_frame({"id": 1, "result": {"userAgent": "codex/0.163.0"}})
    parsed = value.on_frame({"id": 2, "result": {
        "thread": {"id": "parent-thread"}, "approvalPolicy": "never",
        "sandbox": {"type": "workspaceWrite"},
    }})
    assert not parsed.fatal and value.handshake_done
    return value


def turn(session, identity="parent-turn", completed=False):
    return session.on_frame({"method": "turn/completed" if completed else "turn/started",
                             "params": {"threadId": session.thread_id, "turn": {
                                 "id": identity, "status": "completed" if completed else "inProgress",
                                 "items": [],
                             }}})


def activity(session, kind="completed", identity="receipt-item", child="child-thread",
             parent="parent-thread", parent_turn="parent-turn", method="item/completed"):
    return session.on_frame({"method": method, "params": {
        "threadId": parent, "turnId": parent_turn,
        "item": {"type": "subAgentActivity", "id": identity,
                 "kind": kind, "agentThreadId": child},
    }})


def receipt(session):
    turn(session)
    activity(session, "started", "spawn-item")
    turn(session, completed=True)
    activity(session)


def bind(session, request, identity):
    return session.on_frame({"id": request["id"], "result": {"turn": {"id": identity}}})


def claim(session, token=10):
    return json.loads(session.continuation_lines(token)[0])


def test_measured_replay_receipt_after_terminal(session):
    fixture = Path(__file__).parent / "fixtures/codex-app-server/delegated-completion.jsonl"
    for line in fixture.read_text().splitlines():
        parsed = session.on_frame(json.loads(line)["line"])
        assert not parsed.fatal
    assert session.pending_continuation()
    assert not session.busy and not session.native_work_active()
    request = claim(session)
    text = request["params"]["input"][0]["text"]
    assert "success or that their results were consumed" in text
    assert "unfinished work already authorized" in text
    assert "Do not repeat finished work or invent new work" in text
    assert json.loads(text.split("\n")[-1]) == [{
        "receiptId": "receipt-item", "childThreadId": "child-thread", "parentTurnId": "parent-turn",
    }]
    assert not session.pending_continuation()
    assert session.continuation_count == 1
    assert bind(session, request, "actual-turn").settled == ()
    assert turn(session, "actual-turn", completed=True).settled == (10,)


@pytest.mark.parametrize("bad", [None, "", 42, [], {}, "x" * 513])
@pytest.mark.parametrize("field", ["identity", "child", "parent_turn"])
def test_malformed_identity_cannot_establish_ownership(session, bad, field):
    turn(session)
    kwargs = {field: bad}
    assert not activity(session, "started", **kwargs).fatal
    assert not session.owned_children
    assert not session.pending_continuation()


@pytest.mark.parametrize("kwargs", [
    {"parent": "foreign-thread"}, {"child": "parent-thread"},
    {"parent_turn": "unknown-turn"}, {"method": "item/started"},
    {"kind": "resumed"},
])
def test_unverified_activity_does_not_establish_ownership(session, kwargs):
    turn(session)
    args = {"kind": "started", "identity": "spawn-item", **kwargs}
    activity(session, **args)
    activity(session)
    assert not session.owned_children and not session.pending_continuation()


def test_completion_never_establishes_ownership(session):
    turn(session)
    activity(session)
    assert not session.owned_children and not session.pending_continuation()


@pytest.mark.parametrize("params", [[], "text", {"item": []}, {"item": "text"}])
def test_malformed_activity_container_is_ignored(session, params):
    assert not session.on_frame({"method": "item/completed", "params": params}).fatal
    assert not session.pending_continuation()


def test_duplicate_receipt_and_failed_request_never_retry(session):
    receipt(session)
    activity(session)
    request = claim(session)
    parsed = session.on_frame({"id": request["id"], "error": {"message": "rejected"}})
    assert parsed.turn_failed and parsed.settled == (10,)
    activity(session)
    assert not session.pending_continuation()
    assert session.continuation_lines(11) == []
    assert session.continuation_count == 1


def test_failed_write_keeps_claimed_identity_tombstone(session):
    receipt(session)
    claim(session)  # The bridge may fail to write this; admission already spent the receipt.
    activity(session)
    assert not session.pending_continuation()
    assert session.continuation_lines(11) == []
    assert session.continuation_count == 1


@pytest.mark.parametrize("kwargs", [
    {"child": "other-child"}, {"parent_turn": "another-turn"}, {"kind": "started"},
])
def test_conflicting_receipt_identity_fails_closed(session, kwargs):
    receipt(session)
    turn(session, "another-turn", completed=True)
    assert activity(session, **kwargs).fatal
    with pytest.raises(resident_mod.CodexContinuationError, match="conflicts"):
        session.continuation_lines(10)


def test_started_identity_cannot_be_reused_as_receipt(session):
    receipt(session)
    assert activity(session, identity="spawn-item").fatal


def test_same_child_distinct_receipts_coalesce_and_later_receipt_remains_eligible(session):
    receipt(session)
    activity(session, identity="second-receipt")
    request = claim(session)
    payload = json.loads(request["params"]["input"][0]["text"].split("\n")[-1])
    assert [entry["receiptId"] for entry in payload] == ["receipt-item", "second-receipt"]
    bind(session, request, "reconcile-turn")
    turn(session, "reconcile-turn", completed=True)
    activity(session, identity="later-receipt", parent_turn="reconcile-turn")
    assert session.pending_continuation()
    assert claim(session, 11)
    assert session.continuation_count == 2


def test_receipts_wait_through_native_and_held_local_work(session):
    receipt(session)
    turn(session, "native-turn")
    assert session.continuation_lines(10) == []
    assert session.turn_lines("real inbound", 20) == []
    parsed = turn(session, "native-turn", completed=True)
    request = json.loads(parsed.replies[0])
    assert session.pending_continuation()
    assert session.continuation_lines(10) == []
    bind(session, request, "local-turn")
    assert session.continuation_lines(10) == []
    turn(session, "local-turn", completed=True)
    assert session.pending_continuation()
    assert claim(session)


def test_no_claim_until_handshake_ready(session):
    receipt(session)
    session.handshake_done = False
    assert session.continuation_lines(10) == []
    assert session.pending_continuation()


def test_native_race_merged_response_uses_actual_turn_identity(session):
    receipt(session)
    request = claim(session)
    turn(session, "native-race")
    assert not turn(session, "unrelated", completed=True).settled
    assert session.requests
    bind(session, request, "native-race")
    assert turn(session, "native-race", completed=True).settled == (10,)
    assert not turn(session, "native-race", completed=True).settled
    assert session.continuation_count == 1


def test_native_terminal_before_continuation_response_settles_once(session):
    receipt(session)
    request = claim(session)
    turn(session, "native-race")
    assert not turn(session, "native-race", completed=True).settled
    assert bind(session, request, "native-race").settled == (10,)
    assert not bind(session, request, "native-race").settled
    assert not session.busy


def test_attempt_cap_fails_explicitly_without_reusing_receipts(session):
    receipt(session)
    for index in range(resident_mod.CODEX_CONTINUATION_LIMIT):
        if index:
            activity(session, identity=f"receipt-{index}")
        request = claim(session, index + 1)
        bind(session, request, f"reconcile-{index}")
        turn(session, f"reconcile-{index}", completed=True)
    assert session.continuation_count == 32
    activity(session, identity="over-limit")
    with pytest.raises(resident_mod.CodexContinuationError, match="attempt limit"):
        session.continuation_lines(100)
    assert session.continuation_count == 32
    assert session.on_frame({}).fatal


def test_activity_identity_capacity_fails_closed(session, monkeypatch):
    monkeypatch.setattr(resident_mod, "CODEX_IDENTITY_LIMIT", 2)
    receipt(session)
    claim(session)
    assert activity(session, identity="over-capacity").fatal
    assert len(session.child_activity) == 2
    with pytest.raises(resident_mod.CodexContinuationError, match="table is full"):
        session.continuation_lines(11)


def test_receipt_prompt_is_bounded_and_remainder_stays_pending(session):
    receipt(session)
    for index in range(resident_mod.CODEX_CONTINUATION_RECEIPTS):
        activity(session, identity=f"additional-{index}")
    request = claim(session)
    payload = json.loads(request["params"]["input"][0]["text"].split("\n")[-1])
    assert len(payload) == resident_mod.CODEX_CONTINUATION_RECEIPTS
    assert len(session.pending_receipts) == 1
    bind(session, request, "reconciliation")
    turn(session, "reconciliation", completed=True)
    assert len(json.loads(claim(session, 11)["params"]["input"][0]["text"].split("\n")[-1])) == 1


def test_identifiers_are_json_encoded_and_result_text_is_never_repeated(session):
    turn(session)
    activity(session, "started", "spawn-item", child='child\n"instruction')
    turn(session, completed=True)
    activity(session, identity='receipt\n"escape', child='child\n"instruction')
    request = claim(session)
    payload = request["params"]["input"][0]["text"].split("\n")[-1]
    assert json.loads(payload)[0]["receiptId"] == 'receipt\n"escape'
    assert "\\n" in payload and "\\\"" in payload
