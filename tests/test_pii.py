"""PII regression — unit tests for .github/pii_check.py (CI scan is the pii-check job)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github"))

from pii_check import (  # noqa: E402
    check_diff,
    load_patterns,
    parse_patterns,
)

SAMPLE_PATTERNS = "example-agent-name\nexample-hostname\nexample\\.host\\.example\n"


@pytest.fixture(autouse=True)
def _pii_patterns_env(monkeypatch):
    monkeypatch.setenv("PII_PATTERNS", SAMPLE_PATTERNS)


def test_example_agent_name_is_registered_pii_pattern():
    patterns = load_patterns()
    assert "example-agent-name" in patterns


def test_pii_check_catches_example_agent_name_in_added_line():
    diff = "\n".join(
        [
            "diff --git a/example.md b/example.md",
            "+++ b/example.md",
            "+export TELL_OUTBOX_DIR=/var/mailboxes/example-agent-name/.outbox",
        ]
    )
    hits = check_diff(diff, parse_patterns(SAMPLE_PATTERNS))
    assert any(p == "example-agent-name" for p, _ in hits)


def test_load_patterns_requires_env_or_local_file(monkeypatch):
    monkeypatch.delenv("PII_PATTERNS", raising=False)
    local = Path(__file__).resolve().parents[1] / ".github" / "pii-patterns.local.txt"
    if not local.is_file():
        with pytest.raises(FileNotFoundError):
            load_patterns()
