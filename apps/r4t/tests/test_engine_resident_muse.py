import json
import os
from pathlib import Path
import uuid

import pytest

from engines import held_input
from engines import muse_resident as muse
from engines import resident
from engines.run import RunError, _build_argv_template

FIXTURES = Path(__file__).parent / "fixtures" / "muse-msp"


def human_session():
    return json.loads((FIXTURES / "effective-human-policy.json").read_text())["verified"]


def reply(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def ready(tmp_path, **env):
    session = muse.MuseSession(["muse", "serve"], tmp_path, env)
    first = session.on_frame(reply(1, {"schema": {"version": 1},
        "serverInfo": {"version": "1.4.13"}, "grantedCapabilities": ["sessionMcp"],
        "experimentalApi": session.restricted}))
    initialized = {"jsonrpc": "2.0", "method": "initialized"}
    if session.restricted:
        initialized["params"] = {}
    assert json.loads(first.replies[0]) == initialized
    start = json.loads(first.replies[1])
    assert uuid.UUID(start["params"]["commandId"]).version == 7
    session.on_frame(reply(start["id"], {"session": {**human_session(),
        "approvalMode": {"mode": session.mode}, "modelId": session.model}}))
    assert session.handshake_done
    return session, start


@pytest.mark.parametrize("permission, mode, unsandboxed", [
    (None, "onRequest", False), ("auto", "onRequest", False),
    ("ask", "onRequest", False), ("bypass", "allowAll", True),
])
def test_composition_pins_policy_without_changing_one_shot(tmp_path, permission, mode, unsandboxed):
    template, _ = _build_argv_template("muse", model="model-a", effort="high", timeout=900,
        workdir=tmp_path, permissions=permission, resident=True)
    env = {}
    argv = resident.compose_resident_argv(template, env, engine="muse")
    assert argv[:2] == ["muse", "serve"]
    assert ("--disable-sandbox" in argv) == unsandboxed
    assert ("--trust-workspace" in argv) == unsandboxed
    assert "--approval-mode" not in argv and "--yolo" not in argv
    expected = {"R4T_SESSION_MODEL": "model-a", "R4T_SESSION_EFFORT": "high",
                "R4T_SESSION_APPROVAL": mode}
    if permission in (None, "auto"):
        expected["R4T_SESSION_TELL_GRANT"] = "1"
        assert template[template.index("--approval-mode") + 1] == "never"
    assert env == expected
    assert template[1] == "exec"


def test_composer_refuses_unknown_flags():
    with pytest.raises(RunError, match="cannot carry"):
        muse.compose_muse_argv(["muse", "exec", "--mystery", "{prompt}"], {})


def test_session_mcp_is_required_and_scoped(tmp_path):
    session, start = ready(tmp_path, TELL_OUTBOX_DIR="/tmp/outbox")
    config = start["params"]["config"]["mcpServers"]["a8s"]
    assert config["transport"] == "stdio"
    assert config["mode"] == "required"
    assert config["framing"] == "lineDelimitedJson"
    assert config["env"] == {"TELL_OUTBOX_DIR": "/tmp/outbox"}
    assert start["params"]["workspaceRoot"] == str(tmp_path)


@pytest.mark.parametrize("result", [
    {"schema": {"version": 2}, "grantedCapabilities": ["sessionMcp"]},
    {"schema": {"version": 1}, "grantedCapabilities": []},
])
def test_handshake_refuses_incompatible_hosts(tmp_path, result):
    session = muse.MuseSession([], tmp_path, {})
    session.turn_lines("held")
    parsed = session.on_frame(reply(1, result))
    assert parsed.fatal and not parsed.replies and not session.ready


def test_start_must_confirm_policy_and_model(tmp_path):
    session = muse.MuseSession([], tmp_path, {"R4T_SESSION_MODEL": "model-a"})
    session.on_frame(reply(1, {"schema": {"version": 1}, "grantedCapabilities": ["sessionMcp"],
                               "experimentalApi": True}))
    assert session.on_frame(reply(2, {"session": {"sessionId": "s",
        "modelId": "model-a", "approvalMode": {"mode": "allowAll"}}})).fatal


def accepted(session, text="first"):
    start = json.loads(session.turn_lines(text)[0])
    command = start["params"]["commandId"]
    parsed = session.on_frame(reply(start["id"], {"commandId": command, "status": "accepted",
        "disposition": "started", "startedNewTurn": True, "turnId": command}))
    assert not parsed.turn_completed
    session.on_frame(notify("turn/started", turnId=command, commandId=command))
    return start, command


def notify(method, **params):
    return {"method": method, "params": {"sessionId": "session-a", **params}}


def test_ack_is_not_completion_and_queued_turn_waits(tmp_path):
    session, _ = ready(tmp_path, R4T_SESSION_EFFORT="high")
    start, tid = accepted(session)
    assert start["params"]["reasoningEffort"] == "high"
    assert session.turn_lines("second") == []
    assert not session.on_frame(notify("turn/completed", turnId="other", terminal="completed")).turn_completed
    session.on_frame(notify("item/completed", item={"kind": "agentMessage", "itemId": "a", "turnId": tid, "text": "answer"}))
    parsed = session.on_frame(notify("turn/completed", turnId=tid, terminal="completed"))
    assert parsed.turn_completed and not parsed.turn_failed
    assert parsed.assistant_text == "answer"
    assert json.loads(parsed.replies[0])["params"]["input"][0]["text"] == "second"
    assert not session.on_frame(notify("turn/completed", turnId=tid, terminal="completed")).turn_completed


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_terminal_failure_is_explicit(tmp_path, terminal):
    session, _ = ready(tmp_path)
    _, tid = accepted(session)
    assert session.turn_lines("held second turn") == []
    parsed = session.on_frame(notify("turn/completed", turnId=tid, terminal=terminal))
    assert parsed.turn_completed and parsed.turn_failed and parsed.fatal
    assert not parsed.replies and not session.ready and session.error
    assert list(session.held) == ["held second turn"]
    assert not session.on_frame(notify("turn/completed", turnId=tid, terminal=terminal)).turn_completed


def test_start_error_settles_once(tmp_path):
    session, _ = ready(tmp_path)
    frame = json.loads(session.turn_lines("one")[0])
    assert session.turn_lines("held second turn") == []
    error = {"id": frame["id"], "error": {"message": "auth expired"}}
    parsed = session.on_frame(error)
    assert parsed.turn_failed and parsed.turn_completed and parsed.fatal
    assert not parsed.replies and not session.ready and session.error
    assert list(session.held) == ["held second turn"]
    assert not session.on_frame(error).turn_completed


def test_cancel_targets_admitted_turn_without_releasing_queue(tmp_path):
    session, _ = ready(tmp_path)
    assert session.cancel_line() is None
    _, tid = accepted(session)
    session.turn_lines("held")
    cancel = json.loads(session.cancel_line())
    assert cancel["method"] == "turn/cancel" and cancel["params"]["turnId"] == tid
    assert uuid.UUID(cancel["params"]["commandId"]).version == 7
    parsed = session.on_frame(notify("turn/completed", turnId=tid, terminal="cancelled"))
    assert parsed.turn_completed and parsed.turn_failed and not parsed.replies
    assert not parsed.fatal and not session.error


# Sanitized payloads from Muse 1.4.13-R6996.1's exported MSP schema:
# ApprovalRequestParams, ApprovalUpdatedParams and UserInputRequestParams.
def approval_payload(tid, index=0):
    return {"sessionId": "session-a", "approvalId": "approval-a",
        "currentRequirementId": {"approvalId": "approval-a", "sourceIndex": index},
        "availableChoices": [
            {"choiceId": "opaque-denial", "decision": "denied", "scope": "once", "label": "Reject"},
            {"choiceId": "save-grant", "decision": "approvedPolicyAmendment",
             "scope": "localPersistent", "label": "Always allow"}],
        "itemId": "item-a", "judgeEscalated": False, "protectedWrite": False,
        "rawArgs": "{}", "subject": {"kind": "tool", "toolName": "example"},
        "taskId": "task-a", "toolCallId": "call-a", "toolName": "example",
        "turnId": tid, "viewCursor": "cursor-a", "sourceRange": source_range()}


def source_range():
    return {"stream": {"kind": "session", "id": "session-a"},
            "first": {"id": "record-a", "sequence": 1},
            "last": {"id": "record-a", "sequence": 1}}


def approval_update(kind, index=0):
    request = approval_payload("unused", index)
    params = {k: request[k] for k in ("sessionId", "approvalId", "availableChoices",
        "currentRequirementId", "subject", "sourceRange", "viewCursor")}
    params["change"] = {"kind": kind}
    if kind == "stageResolved":
        params["change"].update(requirementId={"approvalId": "approval-a", "sourceIndex": 0},
                                choiceId="allow-once", decision="approved")
    elif kind == "policyPersistence":
        params["change"]["status"] = "succeeded"
    elif kind == "restartRecovered":
        params["change"].update(executable=False, reparsed=False)
    return params


def user_input_payload(tid):
    return {"sessionId": "session-a", "userInputId": "input-a", "itemId": "item-a",
        "turnId": tid, "toolCallId": "call-a", "toolName": "request_user_input",
        "viewCursor": "cursor-a", "questions": [{"id": "choice", "header": "Choice",
            "question": "Which option?", "selection": {"mode": "single"},
            "options": [{"label": "First"}, {"label": "Second"}]}]}


@pytest.mark.parametrize("surface", ["approval", "userInput"])
@pytest.mark.parametrize("request_first", [False, True])
def test_interaction_receipt_and_decision_are_separate_and_deduplicated(tmp_path, surface, request_first):
    session, _ = ready(tmp_path, R4T_SESSION_APPROVAL="allowAll")
    _, tid = accepted(session)
    session.turn_lines("held")
    params = approval_payload(tid) if surface == "approval" else user_input_payload(tid)
    request = {"jsonrpc": "2.0", "id": "server-request", "method": surface + "/request", "params": params}
    notification = {"jsonrpc": "2.0", "method": surface + "/requested", "params": params}
    frames = [request, notification] if request_first else [notification, request]
    output = []
    for frame in frames + [request, notification]:
        parsed = session.on_frame(frame)
        assert not parsed.turn_completed and not parsed.fatal and session.busy
        output.extend(json.loads(line) for line in parsed.replies)
    receipts = [frame for frame in output if "result" in frame]
    assert receipts == [{"jsonrpc": "2.0", "id": "server-request", "result": {}}] * 2
    commands = [frame for frame in output if "method" in frame]
    assert len(commands) == 1
    command = commands[0]
    assert uuid.UUID(command["params"]["commandId"]).version == 7
    if surface == "approval":
        assert command["method"] == "approval/decide"
        assert command["params"]["choiceId"] == "opaque-denial"
        assert command["params"]["requirementId"] == params["currentRequirementId"]
        result = {"approvalId": "approval-a", "terminal": True}
    else:
        assert command["method"] == "userInput/cancel"
        assert command["params"]["userInputId"] == "input-a"
        result = {"userInputId": "input-a"}
    result.update(commandId=command["params"]["commandId"], status="accepted")
    assert not session.on_frame(reply(command["id"], result)).turn_completed
    assert session.busy and list(session.held) == ["held"]
    terminal = session.on_frame(notify("turn/completed", turnId=tid, terminal="completed"))
    assert terminal.turn_completed and len(terminal.replies) == 1


def test_updated_requirement_without_turn_id_is_actionable_once(tmp_path):
    session, _ = ready(tmp_path)
    _, tid = accepted(session)
    initial = session.on_frame(notify("approval/requested", **approval_payload(tid)))
    assert len(initial.replies) == 1
    update = approval_update("stageResolved", index=1)
    assert "turnId" not in update
    parsed = session.on_frame(notify("approval/updated", **update))
    decision = json.loads(parsed.replies[0])
    assert decision["params"]["requirementId"] == {"approvalId": "approval-a", "sourceIndex": 1}
    assert not parsed.turn_completed and session.busy
    assert not session.on_frame(notify("approval/updated", **update)).replies
    request = {"id": 200, "method": "approval/request", "params": approval_payload(tid, index=1)}
    assert [json.loads(line) for line in session.on_frame(request).replies] == [reply(200, {})]


@pytest.mark.parametrize("kind", ["reviewStarted", "reviewCompleted", "policyPersistence", "alreadyTerminal", "futureActivity"])
def test_approval_activity_is_not_a_new_decision(tmp_path, kind):
    session, _ = ready(tmp_path, R4T_SESSION_APPROVAL="allowAll")
    accepted(session)
    parsed = session.on_frame(notify("approval/updated", **approval_update(kind)))
    assert not parsed.replies and not parsed.fatal and not parsed.turn_completed and session.busy
    if kind == "alreadyTerminal":
        assert not session.on_frame(notify("approval/requested", **approval_payload("turn-a"))).replies


@pytest.mark.parametrize("opening", ["approval/reviewing", "approval/updated"])
def test_native_review_waits_for_explicit_handoff(tmp_path, opening):
    session, _ = ready(tmp_path, R4T_SESSION_APPROVAL="allowAll")
    _, tid = accepted(session)
    params = approval_payload(tid) if opening == "approval/reviewing" else approval_update("reviewStarted")
    assert not session.on_frame(notify(opening, **params)).replies
    request = {"id": "during-review", "method": "approval/request", "params": approval_payload(tid)}
    assert [json.loads(line) for line in session.on_frame(request).replies] == [reply("during-review", {})]
    assert not session.on_frame(notify("approval/requested", **approval_payload(tid))).replies
    assert not session.on_frame(notify("approval/updated", **approval_update("stageResolved", 1))).replies
    parsed = session.on_frame(notify("approval/updated", **approval_update("reviewCompleted", 1)))
    assert not parsed.replies and not parsed.fatal and not parsed.turn_completed
    parsed = session.on_frame(notify("approval/requested", **approval_payload(tid, 1)))
    assert len(parsed.replies) == 1 and not parsed.turn_completed


@pytest.mark.parametrize("decision", ["denied", "abort"])
@pytest.mark.parametrize("choice_id", ["abort", "opaque-reject"])
def test_native_reject_once_uses_offered_choice_id(tmp_path, decision, choice_id):
    session, _ = ready(tmp_path)
    _, tid = accepted(session)
    params = approval_payload(tid)
    params["availableChoices"] = [
        {"choiceId": "saved-denial", "decision": "deniedPolicyAmendment",
         "scope": "localPersistent", "label": "Always reject"},
        {"choiceId": choice_id, "decision": decision, "scope": "once", "label": "Reject"},
    ]
    parsed = session.on_frame(notify("approval/requested", **params))
    assert not parsed.fatal and not parsed.turn_completed
    command = json.loads(parsed.replies[0])
    assert command["method"] == "approval/decide"
    assert command["params"]["choiceId"] == choice_id
    assert command["params"]["requirementId"] == params["currentRequirementId"]
    assert not session.on_frame(notify("approval/requested", **params)).replies


def test_native_automated_review_completion_does_not_race_its_resolution(tmp_path):
    session, _ = ready(tmp_path, R4T_SESSION_APPROVAL="allowAll")
    _, tid = accepted(session)
    params = approval_payload(tid)
    params["availableChoices"] = [
        {"choiceId": "allow_once", "decision": "approved", "scope": "once", "label": "Allow once"},
        {"choiceId": "abort", "decision": "abort", "scope": "once", "label": "Reject"},
    ]
    params["toolName"] = "mcp__example__tool"
    params["subject"] = {"kind": "tool", "toolName": params["toolName"]}
    frames = [notify("approval/reviewing", **params)]
    for kind in ("reviewStarted", "reviewCompleted"):
        update = approval_update(kind)
        update.update(availableChoices=params["availableChoices"], subject=params["subject"])
        frames.append(notify("approval/updated", **update))
    frames.append(notify("approval/resolved", approvalId="approval-a", decision="approved",
        itemId="item-a", policyResult="allow", resolvedBy="llmJudge", stageEvidence=[],
        turnId=tid, sourceRange=source_range(), viewCursor="cursor-b"))
    for frame in frames:
        parsed = session.on_frame(frame)
        assert not parsed.replies and not parsed.fatal and not parsed.turn_completed
    assert not session.approval_decisions
    assert not session.on_frame(notify("approval/requested", **params)).replies
    assert session.busy


def test_recovered_pending_approval_and_empty_review_completion(tmp_path):
    session, _ = ready(tmp_path, R4T_SESSION_APPROVAL="allowAll")
    accepted(session)
    update = approval_update("reviewCompleted")
    update["availableChoices"] = []
    assert not session.on_frame(notify("approval/updated", **update)).replies
    parsed = session.on_frame(notify("approval/updated", **approval_update("restartRecovered")))
    assert len(parsed.replies) == 1 and not parsed.fatal


@pytest.mark.parametrize("is_request", [False, True])
def test_escalated_presentation_does_not_wait_for_review_completed(tmp_path, is_request):
    session, _ = ready(tmp_path, R4T_SESSION_APPROVAL="allowAll")
    _, tid = accepted(session)
    assert not session.on_frame(notify("approval/reviewing", **approval_payload(tid))).replies
    params = {**approval_payload(tid), "judgeEscalated": True}
    frame = ({"id": "escalated", "method": "approval/request", "params": params}
             if is_request else notify("approval/requested", **params))
    parsed = session.on_frame(frame)
    output = [json.loads(line) for line in parsed.replies]
    if is_request:
        assert output.pop(0) == reply("escalated", {})
    assert len(output) == 1 and output[0]["method"] == "approval/decide"
    assert output[0]["params"]["choiceId"] == "opaque-denial"
    assert session.busy and not parsed.turn_completed
    assert not session.on_frame(notify("approval/updated", **approval_update("reviewCompleted"))).replies


def test_resolved_approval_and_settled_input_are_not_decided_again(tmp_path):
    session, _ = ready(tmp_path)
    _, tid = accepted(session)
    resolved = {"approvalId": "approval-a", "decision": "denied", "itemId": "item-a",
        "policyResult": "deny", "resolvedBy": "user", "stageEvidence": [], "turnId": tid,
        "sourceRange": source_range(), "viewCursor": "cursor-b"}
    settled = {"userInputId": "input-a", "answers": [], "clarification": None,
        "decidedAt": None, "decidedByCommandId": None, "outcome": "cancelled",
        "reason": "declined", "sourceRange": source_range(), "viewCursor": "cursor-b"}
    assert not session.on_frame(notify("approval/resolved", **resolved)).turn_completed
    assert not session.on_frame(notify("userInput/settled", **settled)).turn_completed
    for method, params in (("approval/request", approval_payload(tid)),
                           ("userInput/request", user_input_payload(tid))):
        parsed = session.on_frame({"id": "late", "method": method, "params": params})
        assert [json.loads(line) for line in parsed.replies] == [reply("late", {})]
    assert session.busy


def test_unknown_server_request_and_foreign_session_do_not_change_turn(tmp_path):
    session, _ = ready(tmp_path)
    _, tid = accepted(session)
    for method, params, code in (("unknown/request", {}, -32601),
            ("approval/request", {**approval_payload(tid), "sessionId": "other"}, -32602)):
        parsed = session.on_frame({"id": 300, "method": method, "params": params})
        assert json.loads(parsed.replies[0])["error"]["code"] == code
        assert not parsed.turn_completed and session.busy


def test_missing_denial_fails_closed_and_keeps_held_turn(tmp_path):
    session, _ = ready(tmp_path)
    _, tid = accepted(session)
    session.turn_lines("held")
    params = approval_payload(tid)
    params["availableChoices"] = params["availableChoices"][1:]
    parsed = session.on_frame({"id": "ask", "method": "approval/request", "params": params})
    assert parsed.fatal and [json.loads(line) for line in parsed.replies] == [reply("ask", {})]
    assert list(session.held) == ["held"]


def test_lifecycle_is_adapter_policy():
    assert resident.MUSE_ADAPTER.leads_process_group
    assert resident.MUSE_ADAPTER.cancel_session
    assert resident.MUSE_ADAPTER.eof_grace_seconds == 10
    assert resident.CURSOR_ADAPTER.eof_grace_seconds == 1.5
    assert resident.CURSOR_ADAPTER.term_grace_seconds == 2


def test_server_may_queue_submit_after_prior_terminal(tmp_path):
    session, _ = ready(tmp_path)
    frame = json.loads(session.turn_lines("next")[0])
    command = frame["params"]["commandId"]
    parsed = session.on_frame(reply(frame["id"], {"commandId": command, "status": "accepted",
        "disposition": "queued", "startedNewTurn": False, "turnId": command}))
    assert not parsed.fatal and not parsed.turn_completed
    assert session.cancel_line() is None
    session.on_frame(notify("turn/started", commandId=command, turnId=command))
    assert session.cancel_line() is not None


def test_closed_session_fails_instead_of_waiting_forever(tmp_path):
    session, _ = ready(tmp_path)
    accepted(session)
    assert session.on_frame(notify("session/closed", reason="unloaded")).fatal


@pytest.mark.parametrize("ack_first", [False, True])
def test_reclaimed_queued_turn_fails_without_releasing_held_input(tmp_path, ack_first):
    session, _ = ready(tmp_path)
    start = json.loads(session.turn_lines("queued")[0])
    command = start["params"]["commandId"]
    ack = reply(start["id"], {"commandId": command, "status": "accepted",
        "disposition": "queued", "startedNewTurn": False, "turnId": command})
    if ack_first:
        session.on_frame(ack)
    else:
        assert session.turn_id is None
    session.turn_lines("held")
    params = {"commandId": command, "turnId": command,
              "sourceRange": source_range(), "viewCursor": "cursor-b"}
    for mismatch in ({"sessionId": "other"}, {"commandId": "other"}):
        ignored = session.on_frame(notify("turn/unqueued", **{**params, **mismatch}))
        assert not ignored.fatal and not ignored.turn_completed and session.busy
    parsed = session.on_frame(notify("turn/unqueued", **params))
    assert parsed.turn_completed and parsed.turn_failed and parsed.fatal
    assert not parsed.replies and not session.ready and not session.busy
    assert list(session.held) == ["held"]
    assert not session.on_frame(notify("turn/unqueued", **params)).turn_completed
    assert not session.on_frame(ack).turn_completed
    assert not session.ready and list(session.held) == ["held"]


@pytest.mark.parametrize("turn_id", [None, "", 123, "inconsistent"])
def test_unqueue_refuses_missing_or_inconsistent_turn_id(tmp_path, turn_id):
    session, _ = ready(tmp_path)
    _, command = accepted(session)
    parsed = session.on_frame(notify("turn/unqueued", commandId=command, turnId=turn_id,
                                    sourceRange=source_range(), viewCursor="cursor-b"))
    assert parsed.fatal and not parsed.replies and not session.ready


@pytest.mark.parametrize("busy", [False, True])
def test_view_gap_closes_session_without_replay_or_queue_release(tmp_path, busy):
    session, _ = ready(tmp_path)
    if busy:
        accepted(session)
        session.turn_lines("held")
    params = {"after": "cursor-a", "next": "cursor-c"}
    assert not session.on_frame(notify("view/gap", sessionId="other", **params)).fatal
    parsed = session.on_frame(notify("view/gap", **params))
    assert parsed.fatal and not parsed.replies and not session.ready
    assert "lost session events" in parsed.error_text


@pytest.mark.parametrize("error", [None, "policy refused"])
def test_check_scans_serve_help_and_uses_no_turn_probe(tmp_path, monkeypatch, error):
    from engines import check
    from conftest import write_path_executable

    calls = []
    fake = write_path_executable(tmp_path / "bin", "muse", "pass\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(check, "interpreter_gap", lambda *_, **k: None)
    monkeypatch.setattr(check, "_version", lambda *_: "1.4.13")
    monkeypatch.setattr(check, "_run", lambda launch, args: (calls.append(args) or (0, "Usage: muse serve [OPTIONS]\n--disable-request-user-input")))
    def probe(argv, cwd, env):
        assert argv[:2] == [str(fake), "serve"]
        assert env["R4T_SESSION_APPROVAL"] == "onRequest"
        assert env["R4T_SESSION_TELL_GRANT"] == "1"
        return {"version": "1.4.13", **({"error": error} if error else {})}
    monkeypatch.setattr(muse, "probe_muse", probe)
    report = check.check_engine("muse", resident=True, workdir=tmp_path)
    assert calls == [["serve", "--help"]]
    assert report.method == "MSP handshake"
    assert report.verdict == (check.REJECTED if error else check.ACCEPTED)


def test_definition_hooks_select_resident_policy():
    from pathlib import Path
    defs = Path(__file__).resolve().parents[2] / "a8s" / "definitions"
    for name, bypass in (("engine-muse-resident", False), ("engine-muse-resident-unrestricted", True)):
        definition = json.loads((defs / (name + ".json")).read_text())
        for argv in (definition["invoke"], definition["batch"]["invoke"], definition["idle"]["invoke"]):
            assert "--resident" in argv
            assert ("--permissions=bypass" in argv or
                    ("--permissions" in argv and argv[argv.index("--permissions") + 1] == "bypass")) == bypass
        assert "stop" in definition["stop"]["invoke"]
        assert "status" in definition["status"]["invoke"]


def test_probe_keeps_cli_environment_and_filters_private_session_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MUSE_TEST_INHERITED", "kept")
    monkeypatch.setenv("A8S_TURN_ENVELOPES", "private")
    calls = []
    def capture(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        raise OSError("test boundary")
    monkeypatch.setattr(muse.subprocess, "Popen", capture)
    result = muse.probe_muse(["muse", "serve"], tmp_path,
                             {"R4T_SESSION_APPROVAL": "onRequest", "MUSE_TEST_OVERRIDE": "explicit",
                              "R4T_SESSION_TELL_GRANT": "1"})
    assert "test boundary" in result["error"]
    argv, child = calls[0]
    assert "--no-session-log" not in argv
    assert child["HOME"] == str(tmp_path)
    assert child["MUSE_TEST_INHERITED"] == "kept"
    assert child["MUSE_TEST_OVERRIDE"] == "explicit"
    assert not any(key.startswith("R4T_SESSION_") for key in child)
    assert "A8S_TURN_ENVELOPES" not in child


def test_locally_initiated_settled_host_shutdown_is_not_a_turn(tmp_path):
    session, _ = ready(tmp_path)
    session.begin_shutdown()
    assert session.closing
    parsed = session.on_frame(notify('session/closed', reason='hostShutdown', viewCursor=None))
    assert not parsed.fatal and not parsed.turn_completed and not parsed.turn_failed
    assert not parsed.replies and not session.ready
    # Expected closure does not silence a subsequent real session error.
    assert session.on_frame(notify('view/gap', after='cursor-a', next='cursor-c')).fatal


@pytest.mark.parametrize('state', [
    'unsolicited', 'active', 'held', 'handshake', 'late_active', 'late_held',
])
def test_host_shutdown_requires_local_settled_handshake(tmp_path, state):
    session, _ = ready(tmp_path)
    if state == 'active':
        accepted(session)
    elif state == 'held':
        session.held.append('held')
    elif state == 'handshake':
        session.handshake_done = False
    if state != 'unsolicited':
        session.begin_shutdown()
    if state == 'late_active':
        accepted(session)
    elif state == 'late_held':
        session.held.append('late held')
    held = list(session.held)
    parsed = session.on_frame(notify('session/closed', reason='hostShutdown', viewCursor='v'))
    assert parsed.fatal and not parsed.turn_completed and not parsed.replies
    assert list(session.held) == held


@pytest.mark.parametrize('reason', ['idle', 'leave', 'future-reason', 'HostShutdown', None])
def test_expected_shutdown_only_accepts_exact_host_reason(tmp_path, reason):
    session, _ = ready(tmp_path)
    session.begin_shutdown()
    ignored = session.on_frame(notify('session/closed', sessionId='foreign',
                                     reason=reason, viewCursor=None))
    assert not ignored.fatal and session.ready
    parsed = session.on_frame(notify('session/closed', reason=reason, viewCursor=None))
    assert parsed.fatal and not parsed.turn_completed


def native_tell(tid):
    # Muse 1.4.13 human-review capture; opaque IDs sanitized, RPC receipt ID supplied.
    frame = json.loads((FIXTURES / "human-tell-denied.json").read_text())[0]
    frame["params"]["turnId"] = tid
    return frame


def decisions(parsed):
    return [frame["params"] for line in parsed.replies
            if (frame := json.loads(line)).get("method") == "approval/decide"]


def tell_session(tmp_path):
    session, _ = ready(tmp_path, R4T_SESSION_TELL_GRANT="1")
    _, tid = accepted(session)
    return session, tid


@pytest.mark.parametrize("permission", ["ask", "bypass"])
def test_explicit_permissions_clear_inherited_tell_grant(tmp_path, permission):
    template, _ = _build_argv_template("muse", model=None, timeout=30,
        workdir=tmp_path, permissions=permission, resident=True)
    env = {"R4T_SESSION_TELL_GRANT": "1"}
    muse.compose_muse_argv(template, env)
    assert "R4T_SESSION_TELL_GRANT" not in env


@pytest.mark.parametrize("bypass", [False, True])
def test_profile_negotiation_preserves_bypass(tmp_path, bypass):
    session = muse.MuseSession([], tmp_path, {"R4T_SESSION_APPROVAL": "allowAll"} if bypass else {})
    capabilities = json.loads(session.opening_lines()[0])["params"]["capabilities"]
    assert capabilities.get("experimentalApi") is (None if bypass else True)
    session, start = ready(tmp_path, R4T_SESSION_APPROVAL="allowAll" if bypass else "onRequest")
    assert start["params"].get("permissionProfile") == (None if bypass else ":ask-me")


@pytest.mark.parametrize("experimental", [None, False, "true", 1])
def test_restricted_handshake_requires_experimental_profile_surface(tmp_path, experimental):
    session = muse.MuseSession([], tmp_path, {})
    session.turn_lines("held")
    parsed = session.on_frame(reply(1, {"schema": {"version": 1},
        "grantedCapabilities": ["sessionMcp"], "experimentalApi": experimental}))
    assert parsed.fatal and not parsed.replies and list(session.held) == ["held"]


@pytest.mark.parametrize("mutation", [
    "missing", "auto-review", "profile", "authority", "attached", "filesystem",
    "network", "approval", "reviewer", "extra-axis", "effective-mode",
])
def test_effective_human_policy_is_required_before_prompt_release(tmp_path, mutation):
    session = muse.MuseSession([], tmp_path, {})
    session.turn_lines("held")
    session.on_frame(reply(1, {"schema": {"version": 1},
        "grantedCapabilities": ["sessionMcp"], "experimentalApi": True}))
    value = human_session()
    state = value["permissionState"]
    if mutation == "missing":
        value.pop("permissionState")
    elif mutation == "auto-review":
        value = json.loads((FIXTURES / "effective-human-policy.json").read_text())["unverified"]
    elif mutation in ("profile", "authority", "attached"):
        state[{"profile": "profileId", "authority": "reviewerAuthority", "attached": "reviewerAttached"}[mutation]] = None
    elif mutation == "extra-axis":
        state["axes"]["unknown"] = "extra"
    elif mutation == "effective-mode":
        value["approvalMode"]["mode"] = "allowAll"
    else:
        state["axes"][mutation] = "other"
    parsed = session.on_frame(reply(2, {"session": value}))
    assert parsed.fatal and not parsed.replies and not session.ready
    assert list(session.held) == ["held"] and not session.human_policy_verified


@pytest.mark.parametrize("attached", [False, True])
def test_effective_human_authority_accepts_boolean_attachment(tmp_path, attached):
    session = muse.MuseSession([], tmp_path, {})
    session.turn_lines("held")
    session.on_frame(reply(1, {"schema": {"version": 1},
        "grantedCapabilities": ["sessionMcp"], "experimentalApi": True}))
    value = human_session()
    value["permissionState"]["reviewerAttached"] = attached
    parsed = session.on_frame(reply(2, {"session": value}))
    assert not parsed.fatal and session.human_policy_verified
    assert json.loads(parsed.replies[0])["method"] == "turn/start"


@pytest.mark.parametrize("notification_first", [False, True])
def test_native_tell_request_grants_once_before_item_event(tmp_path, notification_first):
    session, tid = tell_session(tmp_path)
    request = native_tell(tid)
    for choice in request["params"]["availableChoices"]:
        if choice["decision"] == "approved":
            choice["choiceId"] = "opaque-once"
    notification = {"method": "approval/requested", "params": request["params"]}
    output = []
    for frame in ([notification, request] if notification_first else [request, notification]):
        parsed = session.on_frame(frame)
        assert not parsed.fatal and not parsed.error_text
        output.extend(decisions(parsed))
    assert len(output) == 1 and output[0]["choiceId"] == "opaque-once"
    assert output[0]["requirementId"] == request["params"]["currentRequirementId"]
    for frame in json.loads((FIXTURES / "human-tell-denied.json").read_text())[2:]:
        frame["params"]["turnId"] = tid
        if "item" in frame["params"]:
            frame["params"]["item"]["turnId"] = tid
        assert not decisions(session.on_frame(frame))
    assert not decisions(session.on_frame(request))


@pytest.mark.parametrize("mutation", [
    "tool", "subject-tool", "subject-kind", "subject-missing", "bare-name", "title-only",
    "subagent", "protected", "escalated", "missing-flags", "wrong-turn", "item-id",
    "call-id", "long-call-id", "nonzero-stage", "no-allow-once", "persistent-only",
])
def test_only_exact_native_parent_tell_identity_gets_once_grant(tmp_path, mutation):
    session, tid = tell_session(tmp_path)
    frame = native_tell(tid)
    params = frame["params"]
    if mutation == "tool":
        params["toolName"] = "mcp__other__tell"
    elif mutation == "subject-tool":
        params["subject"]["toolName"] = "mcp__other__tell"
    elif mutation == "subject-kind":
        params["subject"]["kind"] = "shell"
    elif mutation == "subject-missing":
        params.pop("subject")
    elif mutation == "bare-name":
        params["toolName"] = params["subject"]["toolName"] = "tell"
    elif mutation == "title-only":
        params.pop("toolName")
        params.pop("subject")
        params.update(title=muse.TELL_TOOL, rawArgs=json.dumps({"toolName": muse.TELL_TOOL}))
    elif mutation == "subagent":
        params["subagentOrigin"] = None
    elif mutation in ("protected", "escalated"):
        params["protectedWrite" if mutation == "protected" else "judgeEscalated"] = True
    elif mutation == "missing-flags":
        params.pop("protectedWrite")
    elif mutation == "wrong-turn":
        params["turnId"] = "old-turn"
    elif mutation in ("item-id", "call-id", "long-call-id"):
        params["itemId" if mutation == "item-id" else "toolCallId"] = (
            "x" * (muse.MAX_PERMISSION_CALL_ID_LENGTH + 1) if mutation == "long-call-id" else "")
    elif mutation == "nonzero-stage":
        params["currentRequirementId"]["sourceIndex"] = 1
    else:
        params["availableChoices"] = [choice for choice in params["availableChoices"]
                                       if choice["decision"] != "approved"]
        if mutation == "persistent-only":
            params["availableChoices"].insert(0, {"choiceId": "save", "decision": "approved", "scope": "session"})
    assert decisions(session.on_frame(frame))[0]["choiceId"] == "abort"


@pytest.mark.parametrize("state", ["idle", "not-started", "cancelled", "closing", "ask", "bypass", "foreign"])
def test_tell_grant_requires_standard_started_active_session(tmp_path, state):
    session, tid = tell_session(tmp_path)
    frame = native_tell(tid)
    if state == "idle":
        session.on_frame(notify("turn/completed", turnId=tid, terminal="completed"))
    elif state == "not-started":
        session.turn_started = False
    elif state == "cancelled":
        session.cancel_line()
    elif state == "closing":
        session.closing = True
    elif state in ("ask", "bypass"):
        session, _ = ready(tmp_path, R4T_SESSION_APPROVAL="onRequest" if state == "ask" else "allowAll")
        _, tid = accepted(session)
        frame = native_tell(tid)
    elif state == "foreign":
        frame["params"]["sessionId"] = "other"
    parsed = session.on_frame(frame)
    output = decisions(parsed)
    if state == "foreign":
        assert output == []
    else:
        assert len(output) == 1 and output[0]["choiceId"] == "abort"


def test_stage_update_never_grants_even_with_complete_tell_identity(tmp_path):
    session, tid = tell_session(tmp_path)
    params = native_tell(tid)["params"]
    params["change"] = {"kind": "stageResolved"}
    parsed = session.on_frame(notify("approval/updated", **params))
    assert decisions(parsed)[0]["choiceId"] == "abort"


@pytest.mark.parametrize("prior", ["matching", "conflicting", "terminal", "idle", "other-turn", "other-item"])
def test_prior_tool_identity_can_only_narrow_tell_permission(tmp_path, prior):
    session, tid = tell_session(tmp_path)
    item = {"kind": "toolCall", "tool": muse.TELL_TOOL, "callId": "call-a",
            "itemId": "item-a", "turnId": tid, "status": "inProgress"}
    if prior == "conflicting":
        item["tool"] = "mcp__other__tell"
    elif prior == "terminal":
        item["status"] = "rejected"
    elif prior == "idle":
        session.busy = False
    elif prior == "other-turn":
        item["turnId"] = "old-turn"
    elif prior == "other-item":
        item["itemId"] = "other-item"
    session.on_frame(notify("item/updated", item=item))
    session.busy = True
    parsed = session.on_frame(native_tell(tid))
    assert decisions(parsed)[0]["choiceId"] == ("allow_once" if prior == "matching" else "abort")
    corrected = {**item, "tool": muse.TELL_TOOL, "itemId": "item-a", "turnId": tid, "status": "inProgress"}
    session.on_frame(notify("item/started", item=corrected))
    again = native_tell(tid)
    again["params"]["approvalId"] = again["params"]["currentRequirementId"]["approvalId"] = "fresh-approval"
    assert decisions(session.on_frame(again))[0]["choiceId"] == "abort"


def test_consumed_call_id_remains_retired_in_next_turn(tmp_path):
    session, tid = tell_session(tmp_path)
    assert decisions(session.on_frame(native_tell(tid)))[0]["choiceId"] == "allow_once"
    session.on_frame(notify("turn/completed", turnId=tid, terminal="completed"))
    _, next_tid = accepted(session)
    frame = native_tell(next_tid)
    frame["params"]["approvalId"] = frame["params"]["currentRequirementId"]["approvalId"] = "next-approval"
    assert decisions(session.on_frame(frame))[0]["choiceId"] == "abort"


def test_conflicting_request_cannot_reappear_with_a_corrected_identity(tmp_path):
    session, tid = tell_session(tmp_path)
    frame = native_tell(tid)
    frame["params"]["toolName"] = "mcp__other__tell"
    assert decisions(session.on_frame(frame))[0]["choiceId"] == "abort"
    frame = native_tell(tid)
    frame["params"]["approvalId"] = frame["params"]["currentRequirementId"]["approvalId"] = "corrected-approval"
    assert decisions(session.on_frame(frame))[0]["choiceId"] == "abort"


def test_native_deny_fixture_stays_deny_only_in_explicit_ask(tmp_path):
    session, _ = ready(tmp_path)
    _, tid = accepted(session)
    commands = []
    for frame in json.loads((FIXTURES / "human-tell-denied.json").read_text()):
        frame["params"]["turnId"] = tid
        if "item" in frame["params"]:
            frame["params"]["item"]["turnId"] = tid
        parsed = session.on_frame(frame)
        assert not parsed.fatal
        commands.extend(decisions(parsed))
    assert len(commands) == 1 and commands[0]["choiceId"] == "abort"


def test_call_tombstone_saturation_disables_grants_without_eviction_replay(tmp_path):
    session, tid = tell_session(tmp_path)
    for index in range(muse.MAX_PERMISSION_CALLS + 1):
        item = {"kind": "toolCall", "tool": muse.TELL_TOOL, "callId": f"call-{index}",
                "itemId": f"item-{index}", "turnId": tid, "status": "completed"}
        session.on_frame(notify("item/completed", item=item))
    assert session.tell_saturated and len(session.tell_calls) <= muse.MAX_PERMISSION_CALLS
    assert decisions(session.on_frame(native_tell(tid)))[0]["choiceId"] == "abort"


@pytest.mark.parametrize("method, params", [
    ("session/approvalModeChanged", {"mode": "allowAll"}),
    ("session/permissionProfileChanged", {"profileId": ":ask-me"}),
    ("approval/reviewing", {"approvalId": "a"}),
    ("approval/updated", {"approvalId": "a", "change": {"kind": "reviewStarted"}}),
    ("approval/updated", {"approvalId": "a", "change": {"kind": "reviewCompleted"}}),
    ("approval/resolved", {"approvalId": "a", "resolvedBy": "llmJudge"}),
])
def test_restricted_policy_change_or_automatic_review_fails_without_queue_release(tmp_path, method, params):
    session, _ = tell_session(tmp_path)
    session.turn_lines("held")
    assert not session.on_frame(notify(method, sessionId="other", **params)).fatal
    parsed = session.on_frame(notify(method, **params))
    assert parsed.fatal and not parsed.replies and not session.ready
    assert not session.human_policy_verified and list(session.held) == ["held"]


def test_same_mode_notification_keeps_verified_policy(tmp_path):
    session, tid = tell_session(tmp_path)
    assert not session.on_frame(notify("session/approvalModeChanged", mode="onRequest")).fatal
    assert decisions(session.on_frame(native_tell(tid)))[0]["choiceId"] == "allow_once"


def test_bypass_probe_keeps_ephemeral_no_turn_host(monkeypatch, tmp_path):
    calls = []
    def capture(argv, **kwargs):
        calls.append(argv)
        raise OSError("test boundary")
    monkeypatch.setattr(muse.subprocess, "Popen", capture)
    muse.probe_muse(["muse", "serve"], tmp_path, {"R4T_SESSION_APPROVAL": "allowAll"})
    assert calls == [["muse", "serve", "--no-session-log"]]


@pytest.mark.parametrize("prior", ["other-tool", "terminal", "consumed", "resolved", "terminal-without-call"])
def test_durable_item_identity_cannot_be_reused_with_fresh_call_and_approval(tmp_path, prior):
    session, tid = tell_session(tmp_path)
    if prior == "consumed":
        original = native_tell(tid)
        original["params"]["toolCallId"] = "old-call"
        assert decisions(session.on_frame(original))[0]["choiceId"] == "allow_once"
    elif prior == "resolved":
        session.on_frame(notify("approval/resolved", approvalId="old-approval", itemId="item-a",
            turnId=tid, decision="abort", policyResult="deny", resolvedBy="user"))
    else:
        item = {"kind": "toolCall", "tool": muse.TELL_TOOL, "callId": "old-call",
                "itemId": "item-a", "turnId": tid, "status": "inProgress"}
        if prior == "other-tool":
            item["tool"] = "mcp__other__tool"
        else:
            item["status"] = "completed"
        if prior == "terminal-without-call":
            item.pop("callId")
        session.on_frame(notify("item/updated", item=item))
    request = native_tell(tid)
    request["params"]["approvalId"] = request["params"]["currentRequirementId"]["approvalId"] = "fresh-approval"
    assert decisions(session.on_frame(request))[0]["choiceId"] == "abort"


@pytest.mark.parametrize("approval_id", ["approval-a", "other-approval"])
def test_item_approval_join_key_must_match_the_request(tmp_path, approval_id):
    session, tid = tell_session(tmp_path)
    session.on_frame(notify("item/started", item={"kind": "toolCall", "tool": muse.TELL_TOOL,
        "callId": "call-a", "itemId": "item-a", "approvalId": approval_id,
        "turnId": tid, "status": "inProgress"}))
    assert decisions(session.on_frame(native_tell(tid)))[0]["choiceId"] == (
        "allow_once" if approval_id == "approval-a" else "abort")


def test_resolved_approval_identity_cannot_move_to_fresh_item_and_call(tmp_path):
    session, tid = tell_session(tmp_path)
    session.on_frame(notify("approval/resolved", approvalId="approval-a", itemId="old-item",
        turnId=tid, decision="abort", policyResult="deny", resolvedBy="user"))
    request = native_tell(tid)
    assert not decisions(session.on_frame(request))
    request["params"]["approvalId"] = request["params"]["currentRequirementId"]["approvalId"] = "fresh-approval"
    assert decisions(session.on_frame(request))[0]["choiceId"] == "abort"


@pytest.mark.parametrize("index", ["item", "approval"])
def test_partial_native_identity_indices_are_bounded_and_fail_closed(tmp_path, index):
    session, tid = tell_session(tmp_path)
    for number in range(muse.MAX_PERMISSION_CALLS + 1):
        if index == "item":
            session.on_frame(notify("item/completed", item={"kind": "toolCall",
                "tool": muse.TELL_TOOL, "itemId": f"item-{number}", "turnId": tid,
                "status": "completed"}))
        else:
            session.on_frame(notify("approval/resolved", approvalId=f"approval-{number}",
                turnId=tid, decision="abort", policyResult="deny", resolvedBy="user"))
    assert session.tell_saturated
    assert all(len(index) <= muse.MAX_PERMISSION_CALLS for index in (
        session.tell_calls, session.tell_items, session.tell_approvals))
    assert decisions(session.on_frame(native_tell(tid)))[0]["choiceId"] == "abort"


@pytest.mark.parametrize("request_id", [1, 2])
def test_server_request_rpc_id_collision_cannot_complete_or_change_handshake(tmp_path, request_id):
    session = muse.MuseSession([], tmp_path, {"R4T_SESSION_TELL_GRANT": "1"})
    session.turn_lines("held")
    session.on_frame(reply(1, {"schema": {"version": 1},
        "grantedCapabilities": ["sessionMcp"], "experimentalApi": True}))
    request = native_tell("turn-a")
    request["id"] = request_id
    parsed = session.on_frame(request)
    assert not decisions(parsed) and not session.ready and not session.human_policy_verified
    assert 2 in session.pending and list(session.held) == ["held"]
    parsed = session.on_frame(reply(2, {"session": human_session()}))
    start = json.loads(parsed.replies[0])
    tid = start["params"]["commandId"]
    session.on_frame(reply(start["id"], {"commandId": tid, "status": "accepted",
        "disposition": "started", "turnId": tid}))
    session.on_frame(notify("turn/started", commandId=tid, turnId=tid))
    request["params"]["turnId"] = tid
    assert decisions(session.on_frame(request))[0]["choiceId"] == "allow_once"
    assert session.ready and session.human_policy_verified and session.session_id == "session-a"


@pytest.mark.parametrize("ack_first", [False, True])
def test_started_turn_identity_cannot_change_under_the_same_command(tmp_path, ack_first):
    session, _ = ready(tmp_path, R4T_SESSION_TELL_GRANT="1")
    start = json.loads(session.turn_lines("first")[0])
    command = start["params"]["commandId"]
    ack = reply(start["id"], {"commandId": command, "status": "accepted",
        "disposition": "started", "turnId": "turn-a"})
    if ack_first:
        assert not session.on_frame(ack).fatal
    assert not session.on_frame(notify("turn/started", commandId=command, turnId="turn-a")).fatal
    if not ack_first:
        assert not session.on_frame(ack).fatal
    assert not session.on_frame(notify("turn/started", commandId=command, turnId="turn-a")).fatal
    session.turn_lines("held")
    parsed = session.on_frame(notify("turn/started", commandId=command, turnId="turn-b"))
    assert parsed.fatal and not parsed.replies and not session.ready
    assert session.turn_id == "turn-a" and list(session.held) == ["held"]
    assert not session.human_policy_verified
    assert not decisions(session.on_frame(native_tell("turn-b")))


def test_first_started_event_must_match_prior_admission_ack(tmp_path):
    session, _ = ready(tmp_path, R4T_SESSION_TELL_GRANT="1")
    start = json.loads(session.turn_lines("first")[0])
    command = start["params"]["commandId"]
    session.on_frame(reply(start["id"], {"commandId": command, "status": "accepted",
        "disposition": "started", "turnId": "turn-a"}))
    parsed = session.on_frame(notify("turn/started", commandId=command, turnId="turn-b"))
    assert parsed.fatal and not session.turn_started and session.turn_id == "turn-a"
    assert not decisions(session.on_frame(native_tell("turn-b")))


# A held order as a8s renders it at wake: absolute stamp, age measured then.
ORDER = "[2026-10-29 10:42 PDT] alice tells bob (4 seconds ago): ship the build"
QUEUED_AT = 1_000_000.0


def queued(message, at=QUEUED_AT):
    return held_input.QueuedText.from_items([{"message": message, "queued_at": at}])


def submitted(parsed):
    return [json.loads(line)["params"]["input"][0]["text"] for line in parsed.replies]


def finish(session, start, text=""):
    command = start["params"]["commandId"]
    session.on_frame(reply(start["id"], {"commandId": command, "status": "accepted",
        "disposition": "started", "startedNewTurn": True, "turnId": command}))
    session.on_frame(notify("turn/started", turnId=command, commandId=command))
    return session.on_frame(notify("turn/completed", turnId=command, terminal="completed"))


def test_long_held_input_is_redated_and_noted_at_release(tmp_path, monkeypatch):
    session, _ = ready(tmp_path)
    _, tid = accepted(session)
    assert session.turn_lines(queued(ORDER)) == []
    monkeypatch.setattr(held_input, "_now", lambda: QUEUED_AT + 46 * 60)
    parsed = session.on_frame(notify("turn/completed", turnId=tid, terminal="completed"))
    assert submitted(parsed) == [
        "[This input was held 46 minutes while an earlier turn ran. It may already "
        "appear in conversation history you read; check before you act on it twice.]\n"
        "[2026-10-29 10:42 PDT] alice tells bob (46 minutes ago): ship the build"
    ]


def test_briefly_held_input_is_redated_without_a_note(tmp_path, monkeypatch):
    session, _ = ready(tmp_path)
    _, tid = accepted(session)
    session.turn_lines(queued(ORDER))
    monkeypatch.setattr(held_input, "_now", lambda: QUEUED_AT + 30)
    parsed = session.on_frame(notify("turn/completed", turnId=tid, terminal="completed"))
    assert submitted(parsed) == [
        "[2026-10-29 10:42 PDT] alice tells bob (34 seconds ago): ship the build"
    ]


def test_input_submitted_at_once_keeps_its_header_and_gets_no_note(tmp_path, monkeypatch):
    session, _ = ready(tmp_path)
    monkeypatch.setattr(held_input, "_now", lambda: QUEUED_AT + 46 * 60)
    (line,) = session.turn_lines(queued(ORDER))
    assert json.loads(line)["params"]["input"][0]["text"] == ORDER


def test_each_held_input_is_submitted_exactly_once(tmp_path, monkeypatch):
    session, _ = ready(tmp_path)
    _, tid = accepted(session)
    second = ORDER.replace("ship the build", "tag the release")
    session.turn_lines(queued(ORDER))
    session.turn_lines(queued(second, QUEUED_AT + 1))
    monkeypatch.setattr(held_input, "_now", lambda: QUEUED_AT + 40 * 60)
    first_release = session.on_frame(notify("turn/completed", turnId=tid, terminal="completed"))
    (text,) = submitted(first_release)
    assert text.endswith("(40 minutes ago): ship the build")
    assert list(session.held) == [second]
    second_release = finish(session, json.loads(first_release.replies[0]))
    (text,) = submitted(second_release)
    assert text.startswith("[This input was held 39 minutes")
    assert text.endswith("(40 minutes ago): tag the release")
    assert not session.held
    assert not finish(session, json.loads(second_release.replies[0])).replies
