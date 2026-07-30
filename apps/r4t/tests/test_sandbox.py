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
    assert "crew:lead" in report  # conversation section
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
    assert "crew:dev -> crew:lead" in report
    assert "crew:lead -> crew:tester" in report
    assert "BREAKER" not in report


def test_fake_sandbox_mute_member_is_swept_as_quiet():
    report, _ = _run_sandbox("--break", "lead:mute")

    mechanical = _mechanical(report)
    for check in (
        "Silent turn logged",
        "Quiet sweep nudged the leader",
        "Breaker stayed closed",
        "Leader answered the originator",
    ):
        assert check in mechanical
    assert "| FAIL |" not in mechanical
    # Nothing staged, nothing worth relaying, no failed turn — the quiet sweep
    # is the only thing that gets the originator an answer.
    assert "SILENT lead" in report
    assert "QUIET thread=" in report
    assert "nudged leader lead" in report
    assert "BREAKER" not in report
    assert "ANSWERED thread=" in report
