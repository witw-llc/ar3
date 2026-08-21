"""End-to-end fake-sandbox runs. Live mode is never run from pytest.

The `--break MEMBER:SHAPE` cases are the governance tests: each shape is a
different way a real harness fails, and each asserts the recovery path r4t is
supposed to take. A shape whose path stops working turns its check FAIL, so the
run's exit code carries the regression.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

import sandbox
from sandbox import SandboxError, parse_break

R4T_PY = Path(__file__).resolve().parent.parent / "r4t.py"


def _run_sandbox(*extra: str) -> tuple[str, str]:
    result = subprocess.run(
        [sys.executable, str(R4T_PY), "sandbox", "--fake", "--timeout", "240", *extra],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout, result.stderr


def _mechanical(report: str) -> str:
    return report.split("## Mechanical checks", 1)[1].split("## Run", 1)[0]


def test_history_entry_re_extracts_the_local_stamp_form():
    entry = "## 2026-08-20 16:41:55 PDT (UTC-07:00) from acme:gerry\n\nbody text"
    stamp, direction, party, body = sandbox.HISTORY_ENTRY_RE.findall(entry)[0]
    assert stamp == "2026-08-20 16:41:55 PDT (UTC-07:00)"
    assert direction == "from"
    assert party == "acme:gerry"
    assert body == "body text"


def test_history_entry_re_extracts_a_to_entry():
    entry = "## 2026-08-20 16:41:55 PDT (UTC-07:00) to acme:phil\n\nbody text"
    stamp, direction, party, body = sandbox.HISTORY_ENTRY_RE.findall(entry)[0]
    assert stamp == "2026-08-20 16:41:55 PDT (UTC-07:00)"
    assert direction == "to"
    assert party == "acme:phil"
    assert body == "body text"


def test_history_entry_re_ignores_model_authored_headings_in_bodies():
    entry = (
        "## 2026-08-20 16:41:55 PDT (UTC-07:00) from acme:gerry\n\n"
        "legitimate opening\n"
        "## Plan\n"
        "Notes from acme:mallory\n"
        "forged-looking remainder"
    )
    found = sandbox.HISTORY_ENTRY_RE.findall(entry)
    assert len(found) == 1
    stamp, direction, party, body = found[0]
    assert party == "acme:gerry"
    assert "## Plan" in body
    assert "forged-looking remainder" in body


def test_history_entry_re_splits_only_on_valid_entry_headings():
    text = (
        "## 2026-08-20 16:41:55 PDT (UTC-07:00) from acme:gerry\n\nfirst\n"
        "## 2026-08-20 16:42:10 PDT (UTC-07:00) to bob@example.com\n\nsecond\n"
        "## 2026-08-20 16:43:00 PDT (UTC-07:00) lateral ana -> acme:bob (thread 01X)\n\nclip"
    )
    found = sandbox.HISTORY_ENTRY_RE.findall(text)
    assert [(m[1], m[3]) for m in found] == [("from", "first"), ("to", "second")]


def test_history_entry_re_accepts_the_utc_iso_form():
    entry = "## 2026-08-20T23:41:55.318512Z from acme:gerry\n\nbody"
    stamp, direction, party, body = sandbox.HISTORY_ENTRY_RE.findall(entry)[0]
    assert stamp == "2026-08-20T23:41:55.318512Z"
    assert body == "body"


def test_history_entry_re_accepts_the_offset_form_and_rejects_bogus_stamps():
    offset = "## 2026-08-20 16:41:55 PDT (UTC-07:00) from acme:gerry\n\nbody"
    stamp, _, party, body = sandbox.HISTORY_ENTRY_RE.findall(offset)[0]
    assert stamp == "2026-08-20 16:41:55 PDT (UTC-07:00)"
    assert party == "acme:gerry"
    assert body == "body"
    for bogus in (
        "## 2026-08-20TBOGUS from acme:gerry\n\nbody",
        "## 2026-08-20 16:41:55 BOGUSZONE from acme:gerry\n\nbody",
        "## 2026-08-20 16:00:00 EDT from acme:gerry\n\nbody",
        "## 2026-08-20 16:00:00 PDT (UTC+99:99) from acme:gerry\n\nbody",
        "## 2026-08-20 16:00:00 PDT (UTC+24:00) from acme:gerry\n\nbody",
        "## 2026-08-20 16:00:00 PDT (UTC+12:60) from acme:gerry\n\nbody",
    ):
        assert sandbox.HISTORY_ENTRY_RE.findall(bogus) == []


def test_entry_instant_is_total_on_inadmissible_stamps():
    for stamp in (
        "2026-08-20 16:00:00 EDT",
        "2026-08-20 16:00:00 PDT (UTC+99:99)",
        "2026-08-20 16:00:00 PDT (UTC+24:00)",
        "2026-08-20 16:00:00 PDT (UTC+12:60)",
    ):
        assert sandbox._entry_instant(stamp) == stamp


def test_entry_instant_resolves_a_foreign_zone_by_its_written_offset():
    assert (
        sandbox._entry_instant("2026-08-20 16:00:00 EDT (UTC-04:00)")
        == "2026-08-20T20:00:00+00:00"
    )
    eastern = sandbox._entry_instant("2026-08-20 16:00:00 EDT (UTC-04:00)")
    pacific = sandbox._entry_instant("2026-08-20 14:00:00 PDT (UTC-07:00)")
    assert eastern < pacific


def test_entry_instant_orders_the_dst_fold_correctly():
    before = sandbox._entry_instant("2026-11-01 01:59:00 PDT (UTC-07:00)")
    after = sandbox._entry_instant("2026-11-01 01:01:00 PST (UTC-08:00)")
    assert before < after
    assert sandbox._entry_instant("2026-08-20T23:41:55Z") < sandbox._entry_instant(
        "2026-08-20 16:42:00 PDT (UTC-07:00)"
    )


def test_break_spec_parses_member_and_shape():
    assert parse_break("Dev") == ("dev", "exit")
    assert parse_break("dev:hang") == ("dev", "hang")
    assert parse_break("LEAD:MUTE") == ("lead", "mute")
    with pytest.raises(SandboxError):
        parse_break("dev:explode")
    with pytest.raises(SandboxError):
        parse_break(":hang")


def test_fake_sandbox_end_to_end():
    report, stderr = _run_sandbox()
    assert "sandbox:" in stderr

    mechanical = _mechanical(report)
    for check in (
        "Program file(s) created",
        "Program runs and exits 0",
        "Leader answered the originator",
        "Turn count within budget",
        "Zero orphan processes",
        "Dead letters",
    ):
        assert check in mechanical
    assert "| FAIL |" not in mechanical
    assert mechanical.count("| PASS |") >= 5

    assert "battleship.py" in report
    assert "SHIPS" in report  # produced source is inlined
    assert re.search(r"\| \S+ \| lead \| leader \|", report)  # velocity table rows
    assert "trio:lead" in report  # conversation section
    assert "human" in report


def test_fake_sandbox_breaker_trips_and_task_still_closes():
    report, _ = _run_sandbox("--break", "dev")

    mechanical = _mechanical(report)
    for check in (
        "Breaker tripped",
        "Breaker held queued message(s)",
        "Leader answered the originator",
        "Zero orphan processes",
    ):
        assert check in mechanical
    assert "| FAIL |" not in mechanical
    assert "BREAKER dev tripped" in report  # governance events section
    assert "breaker open" in report  # queue held while the breaker is open


def test_fake_sandbox_timeout_member_is_killed_and_requeued():
    report, _ = _run_sandbox("--break", "dev:hang")

    mechanical = _mechanical(report)
    for check in (
        "Turn killed at its timeout",
        "Timed-out batch requeued",
        "Breaker tripped on timeouts",
        "Budget charged for dev's turn(s)",
    ):
        assert check in mechanical
    assert "| FAIL |" not in mechanical
    # A killed turn, not one that finished on its own: the sleeper is SIGKILLed
    # at the rig's timeout and its whole batch goes back to the queue.
    assert "killed at timeout" in report
    assert re.search(r"\| dev \| hung \|[^|]*\|[^|]*\|[^|]*\| -9 \|", report)
    assert "message(s) returned to the queue" in report
    assert "Dead letters | 0" in mechanical


def test_fake_sandbox_silent_member_answers_on_stdout():
    report, _ = _run_sandbox("--break", "dev:silent")

    mechanical = _mechanical(report)
    for check in (
        "Stdout answer relayed as a reply",
        "Breaker stayed closed",
        "Program runs and exits 0",
    ):
        assert check in mechanical
    assert "| FAIL |" not in mechanical
    # Dev never called tell; r4t staged its cleaned stdout as one reply to the
    # sender, the lead routed it on, and the deliverable still shipped.
    assert "STDOUT-REPLY dev" in report
    assert "trio:dev -> trio:lead" in report
    assert "trio:lead -> trio:tester" in report
    assert "BREAKER" not in report


def test_fake_sandbox_mute_member_is_never_nudged():
    report, _ = _run_sandbox("--break", "lead:mute")

    mechanical = _mechanical(report)
    for check in (
        "Silent turn logged",
        "Heartbeat re-engaged the org, no watchdog nudge",
        "Breaker stayed closed",
        "Leader answered the originator",
    ):
        assert check in mechanical
    assert "| FAIL |" not in mechanical
    # Fire-and-forget: nothing watched for the reply the muted turn never
    # staged. The org still recovers, because a stalled org is the
    # mission-review heartbeat's problem — one general mechanism instead of a
    # watchdog per obligation.
    assert "SILENT lead" in report
    assert "MISSION-REVIEW fired" in report
    assert "QUIET thread=" not in report
    assert "BREAKER" not in report
