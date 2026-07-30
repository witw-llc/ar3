"""Subject-strip unit tests — pure function, no fixtures needed."""
from __future__ import annotations

import sys
from pathlib import Path

# Connector dir on sys.path so we can `import gmail_cron`. conftest.py at
# apps/a8s/tests/ already adds apps/a8s/ for the registry import inside
# gmail_cron.
_CONNECTOR_DIR = Path(__file__).resolve().parent.parent.parent.parent / "connectors" / "gmail"
sys.path.insert(0, str(_CONNECTOR_DIR))

from gmail_cron import parse_subject_to_name  # noqa: E402


def test_simple_re():
    assert parse_subject_to_name("Re: NEIL") == "NEIL"


def test_repeated_re():
    assert parse_subject_to_name("Re: Re: NEIL") == "NEIL"


def test_re_no_space():
    assert parse_subject_to_name("RE:NEIL") == "NEIL"


def test_fwd():
    assert parse_subject_to_name("Fwd: NEIL") == "NEIL"


def test_fw_short():
    assert parse_subject_to_name("Fw: NEIL") == "NEIL"


def test_mixed_re_fwd():
    assert parse_subject_to_name("re: fwd: NEIL") == "NEIL"


def test_no_prefix_left_alone():
    # No prefix to strip — caller's resolve_name will reject it.
    assert parse_subject_to_name("NEIL urgent") == "NEIL urgent"


def test_empty_string():
    assert parse_subject_to_name("") == ""


def test_only_whitespace():
    assert parse_subject_to_name("   ") == ""


def test_case_variations():
    assert parse_subject_to_name("rE: NEIL") == "NEIL"
    assert parse_subject_to_name("FWD: neil") == "neil"


def test_re_with_extra_whitespace():
    assert parse_subject_to_name("Re:    NEIL") == "NEIL"


def test_re_followed_by_fwd_followed_by_re():
    assert parse_subject_to_name("Re: Fwd: Re: alpha") == "alpha"


def test_does_not_strip_inside_subject():
    # Only leading prefixes are stripped.
    assert parse_subject_to_name("alpha Re: beta") == "alpha Re: beta"
