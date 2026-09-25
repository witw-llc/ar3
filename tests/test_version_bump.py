"""Version bump gate — unit tests for tools/version-bump.py."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOL = REPO_ROOT / "tools" / "version-bump.py"
_SPEC = importlib.util.spec_from_file_location("version_bump", _TOOL)
version_bump = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(version_bump)


class TestAllowedNext:
    def test_three_forms_from_a_mid_phase_version(self):
        assert version_bump.allowed_next("0.1.95") == {
            "build": "0.1.96",
            "minor": "0.2.0",
            "major": "1.0.0",
        }

    def test_three_forms_from_a_fresh_phase(self):
        assert version_bump.allowed_next("0.2.0") == {
            "build": "0.2.1",
            "minor": "0.3.0",
            "major": "1.0.0",
        }

    def test_rejects_a_malformed_version(self):
        with pytest.raises(ValueError):
            version_bump.allowed_next("0.1")
        with pytest.raises(ValueError):
            version_bump.allowed_next("0.1.x")


class TestCheck:
    @pytest.mark.parametrize(
        "head",
        ["0.1.96", "0.2.0", "1.0.0"],
    )
    def test_each_allowed_form_passes(self, head):
        assert version_bump.check("0.1.95", head) is None

    def test_unchanged_version_fails(self):
        message = version_bump.check("0.1.95", "0.1.95")
        assert message is not None
        assert "0.1.96" in message and "0.2.0" in message and "1.0.0" in message

    def test_skipped_build_fails(self):
        message = version_bump.check("0.1.95", "0.1.97")
        assert message is not None

    def test_minor_bump_that_keeps_a_nonzero_build_fails(self):
        message = version_bump.check("0.1.95", "0.2.1")
        assert message is not None

    def test_major_bump_that_keeps_minor_or_build_fails(self):
        assert version_bump.check("0.1.95", "1.1.0") is not None
        assert version_bump.check("0.1.95", "1.0.1") is not None

    def test_downgrade_fails(self):
        assert version_bump.check("0.1.95", "0.1.94") is not None

    def test_cli_exit_codes(self, capsys):
        assert version_bump.main(["0.1.95", "0.1.96"]) == 0
        assert version_bump.main(["0.1.95", "0.1.95"]) == 1
        assert "Allowed" in capsys.readouterr().err
