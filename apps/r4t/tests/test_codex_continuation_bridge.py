"""Offline bridge integration; synthetic scheduling around measured receipts.

The fixture records ownership and completion after the parent terminal.
These tests do not establish vendor consumption or native restart behavior.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import threading

import pytest

from ar3.wake_outcome import origin_from_env
from engines import resident as resident_mod
from engines import run as engine_run

FIXTURES = Path(__file__).parent / "fixtures" / "codex-app-server"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="fake subprocess is POSIX")


def _run(monkeypatch, tmp_path, scenario):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("A8S_HOME", str(tmp_path / "a8s-home"))
    monkeypatch.delenv("TELL_OUTBOX_DIR", raising=False)
    home = tmp_path / "resident"
    (home / "inbox").mkdir(parents=True)
    record = tmp_path / "wire.jsonl"
    stop = threading.Event()
    all_receipts = threading.Event()
    sessions, submitted, failures, settled = [], [], [], []
    origin = origin_from_env({
        "A8S_TURN_RECIPIENT": "probe",
        "A8S_TURN_ENVELOPES": '["original.json"]',
        "A8S_TURN_OUTCOME_DIR": str(tmp_path / "outcomes"),
    }, "probe")
    resident_mod._enqueue_message(home, "initial message", origin)
    on_frame = resident_mod.CodexSession.on_frame
    pending = resident_mod.CodexSession.pending_continuation
    continuation = resident_mod.CodexSession.continuation_lines
    hook_done = False

    def observe(session, frame):
        nonlocal hook_done
        if not sessions:
            sessions.append(session)
        parsed = on_frame(session, frame)
        settled.extend(parsed.settled)
        if frame.get("method") == "test/barrier":
            # Gate the scheduler until the complete input burst has been
            # applied, so coalescing does not depend on reader timing.
            all_receipts.set()
        item = frame.get("params", {}).get("item", {})
        if item.get("kind") == "completed" and not hook_done:
            hook_done = True
            if scenario == "inbound":
                resident_mod._enqueue_message(home, "real inbound", origin)
            elif scenario == "stop":
                all_receipts.set()
                stop.set()
            elif scenario == "fatal":
                all_receipts.set()
                parsed = replace(parsed, fatal=True, error_text="synthetic fatal receipt")
        return parsed

    def submit(session, token):
        if not all_receipts.is_set():
            return []
        native = session.native_work_active()
        lines = continuation(session, token)
        if scenario == "native" and native:
            assert not lines
            record.with_suffix(".release").touch()
        if lines and scenario == "stop-during-claim":
            stop.set()
        if lines:
            submitted.append({"token": token, "native": native})
        return lines

    monkeypatch.setattr(resident_mod.CodexSession, "on_frame", observe)
    monkeypatch.setattr(resident_mod.CodexSession, "pending_continuation",
                        lambda session: all_receipts.is_set() and pending(session))
    monkeypatch.setattr(resident_mod.CodexSession, "continuation_lines", submit)
    monkeypatch.setattr(resident_mod, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(resident_mod, "report_failure",
                        lambda *args, **kwargs: failures.append((args, kwargs)))
    monkeypatch.setattr(engine_run, "resolve_argv0", lambda argv: [
        sys.executable, str(FIXTURES / "continuation_server.py"), scenario, str(record),
    ])
    if scenario == "writer-probe":
        condition_factory = threading.Condition
        conditions = []

        def observe_condition(*args, **kwargs):
            condition = condition_factory(*args, **kwargs)
            if sys._getframe(1).f_code.co_name == "_run_session":
                conditions.append(condition)
            return condition

        monkeypatch.setattr(threading, "Condition", observe_condition)
        popen = resident_mod.subprocess.Popen

        class ObservedStdin:
            def __init__(self, stream):
                self.stream = stream

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def write(self, text):
                if "[Resident delegated-result reconciliation]" in text:
                    done = threading.Event()

                    def acquire_scheduler_locks():
                        with resident_mod.file_lock(resident_mod._lock_file(home)):
                            with conditions[0]:
                                with sessions[0].lock:
                                    done.set()

                    worker = threading.Thread(target=acquire_scheduler_locks, daemon=True)
                    worker.start()
                    # This write cannot finish until another thread has
                    # acquired every scheduling lock, or the probe fails.
                    if done.wait(2):
                        (tmp_path / "writer-locks-free").touch()
                return self.stream.write(text)

        def observed_popen(*args, **kwargs):
            proc = popen(*args, **kwargs)
            proc.stdin = ObservedStdin(proc.stdin)
            return proc

        monkeypatch.setattr(resident_mod.subprocess, "Popen", observed_popen)
    watchdog = threading.Timer(8, stop.set)
    watchdog.daemon = True
    watchdog.start()
    template, _ = engine_run._build_argv_template(
        "codex", model=None, effort=None, timeout=30, workdir=tmp_path, resident=True,
    )
    try:
        code = resident_mod._run_session(home, {
            "engine": "codex", "dir_path": str(tmp_path), "template": template,
            "agent": "probe", "timeout": 1, "scaffold": True, "memory": "off",
        }, stop, {})
    finally:
        watchdog.cancel()
    assert scenario in {"stop", "stop-during-claim"} or not stop.is_set(), "bridge did not settle before watchdog"
    rows = [json.loads(line) for line in record.read_text().splitlines()]
    requests = [row["frame"] for row in rows if row["kind"] == "in"
                and row["frame"].get("method") == "turn/start"]
    texts = [request["params"]["input"][0]["text"] for request in requests]
    return code, texts, submitted, settled, failures, sessions[0], home, rows


@pytest.mark.parametrize("scenario", ["duplicate", "coalesced", "native", "merged", "terminal-first"])
def test_owned_completion_reconciles_once_then_wraps(monkeypatch, tmp_path, scenario):
    code, texts, submissions, settled, failures, session, home, rows = _run(
        monkeypatch, tmp_path, scenario)
    assert code == 0
    assert len(texts) == 3
    assert "initial message" in texts[0]
    assert texts[1].startswith("[Resident delegated-result reconciliation]")
    if scenario == "native":
        assert (tmp_path / "wire.release").exists()
    assert texts[-1].startswith("[Resident session quiet")
    assert len(submissions) == 1 and not submissions[0]["native"]
    assert settled.count(submissions[0]["token"]) == 1
    assert session.continuation_count == 1 and not session.pending_continuation()
    assert not session.busy and not session.native_work_active()
    assert resident_mod.read_state(home)["turns"] == 0
    assert failures == []
    assert rows[-1]["kind"] == "eof"


@pytest.mark.parametrize("scenario", ["generic", "unknown"])
def test_unowned_or_generic_completion_does_not_schedule(monkeypatch, tmp_path, scenario):
    code, texts, submissions, _, failures, session, _, _ = _run(monkeypatch, tmp_path, scenario)
    assert code == 0 and len(texts) == 2
    assert texts[-1].startswith("[Resident session quiet")
    assert submissions == [] and session.continuation_count == 0 and failures == []


def test_real_inbound_precedes_pending_reconciliation(monkeypatch, tmp_path):
    code, texts, submissions, settled, failures, _, _, _ = _run(monkeypatch, tmp_path, "inbound")
    assert code == 0 and len(texts) == 4
    assert texts[1] == "real inbound"
    assert texts[-1].startswith("[Resident session quiet")
    assert len(submissions) == 1 and settled.count(submissions[0]["token"]) == 1
    assert failures == []


@pytest.mark.parametrize("scenario", ["stop", "fatal"])
def test_stop_or_fatal_suppresses_pending_reconciliation(monkeypatch, tmp_path, scenario):
    code, texts, submissions, _, _, _, _, _ = _run(monkeypatch, tmp_path, scenario)
    assert len(texts) == 1 and submissions == []
    if scenario == "fatal":
        assert code == 1


def test_rejected_reconciliation_settles_without_replay_or_turn_origin(monkeypatch, tmp_path):
    code, texts, submissions, settled, failures, session, home, _ = _run(
        monkeypatch, tmp_path, "failed")
    assert code == 1 and len(submissions) == 1
    assert len(texts) == 3 and texts[-1].startswith("[Resident session quiet")
    assert settled.count(submissions[0]["token"]) == 1
    assert session.continuation_count == 1 and not session.pending_continuation()
    assert resident_mod.read_state(home)["turns"] == 0
    # The session may report failure against its last external wake. The
    # synthetic turn itself must not inherit that wake's origin.
    assert all(kwargs.get("session") is True for _, kwargs in failures)


def test_stop_during_claim_suppresses_the_wire_write(monkeypatch, tmp_path):
    code, texts, submissions, _, _, session, _, _ = _run(
        monkeypatch, tmp_path, "stop-during-claim")
    assert code == 1 and len(texts) == 1
    assert len(submissions) == 1 and session.continuation_count == 1
    assert not session.pending_continuation()


def test_reconciliation_pipe_write_releases_all_scheduler_locks(monkeypatch, tmp_path):
    code, texts, submissions, _, failures, _, _, _ = _run(
        monkeypatch, tmp_path, "writer-probe")
    assert code == 0 and len(texts) == 3 and len(submissions) == 1
    assert (tmp_path / "writer-locks-free").exists()
    assert failures == []
