"""Muse is wired, not merely declared.

The suite's recurring defect is configuration that parses, validates and
documents cleanly with nothing behind it. These tests are the other half of
adding an engine: each one fails if a specific piece of the wiring is removed,
so `muse` cannot decay into a name the tables know and nothing honours.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import engines
from engines import run as engine_run
from rig import (
    HARNESS_PRESETS,
    PERMISSION_TRANSLATION,
    allowed_tools_unsupported_reason,
    continue_unsupported_reason,
    mcp_unsupported_reason,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFINITIONS = REPO_ROOT / "apps" / "a8s" / "definitions"


def argv_for(**kwargs):
    base = dict(model=None, timeout=900, workdir=Path("/tmp"))
    base.update(kwargs)
    return engine_run.build_argv("muse", prompt="PROMPT", **base)


class TestPreset:
    def test_muse_is_a_preset_and_a_run_engine(self):
        assert "muse" in HARNESS_PRESETS
        assert "muse" in engine_run.RUN_ENGINES

    def test_capabilities_are_run_and_check_without_quota(self):
        # muse exposes no usage surface, so the module implements no quota
        # verb and the registry must not advertise one.
        assert engines.capabilities("muse") == ["run", "check"]
        assert engines.capability("muse", "quota") is None

    def test_quota_refuses_by_name_instead_of_raising_attributeerror(self):
        # The dispatch guard: engines.quota() calls MODULES[engine].quota()
        # directly, so a module without one crashes unless the caller checks.
        with pytest.raises(engines.QuotaError) as excinfo:
            engines.quota("muse")
        assert "no quota verb" in str(excinfo.value)

    def test_headless_invocation_is_exec_with_a_positional_prompt(self):
        assert argv_for() == [
            "muse", "exec",
            "--approval-mode", "never",
            "--user-input-auto-resolve",
            "PROMPT",
        ]

    def test_model_is_spliced_at_the_exec_anchor(self):
        argv = argv_for(model="muse-spark-1.2")
        assert argv[:4] == ["muse", "exec", "--model", "muse-spark-1.2"]


class TestPermissionTranslation:
    def test_muse_is_registered_in_the_translation_table(self):
        assert PERMISSION_TRANSLATION["muse"].anchor == "exec"

    def test_ask_returns_muse_to_its_own_default(self):
        # Both flags go: --approval-mode never AND the auto-resolve flag that
        # exists only to keep an unattended turn from blocking on a prompt.
        assert argv_for(permissions="ask") == ["muse", "exec", "PROMPT"]

    def test_auto_is_what_the_preset_already_carries(self):
        assert argv_for(permissions="auto") == argv_for()

    def test_bypass_is_yolo_and_drops_the_approval_flag(self):
        argv = argv_for(permissions="bypass")
        assert "--yolo" in argv
        assert "--approval-mode" not in argv
        # --yolo disables approvals and the sandbox; the auto-resolve flag is
        # about unattendedness, not permission, so it stays.
        assert "--user-input-auto-resolve" in argv


class TestRefusalsNameTheirReason:
    def test_continue_is_refused_because_resume_is_interactive(self):
        with pytest.raises(engine_run.RunError) as excinfo:
            argv_for(continue_conversation=True)
        message = str(excinfo.value)
        assert "muse cannot continue" in message
        assert "session picker" in message

    def test_continue_reason_is_recorded_for_muse(self):
        assert "picker" in continue_unsupported_reason("muse")

    def test_allowed_tools_is_refused_with_a_reason(self):
        with pytest.raises(Exception) as excinfo:
            argv_for(allowed_tools="Read Write")
        assert "muse" in str(excinfo.value)
        assert "permission profile" in allowed_tools_unsupported_reason("muse")

    def test_mcp_reason_says_muse_serves_msp_rather_than_consuming_mcp(self):
        reason = mcp_unsupported_reason("muse")
        assert "MSP" in reason
        assert HARNESS_PRESETS["muse"].get("mcp") is None


class TestCheckProbe:
    def test_a_run_engine_has_a_check_probe(self):
        # Without this, `r4t engine muse check` raises KeyError instead of
        # reporting — a verb the registry advertises and cannot perform.
        from engines import check as engine_check

        assert "muse" in engine_check.PROBES
        probe = engine_check.PROBES["muse"]
        assert probe.help_binary == "muse"
        assert probe.help_argv == ("exec", "--help")
        # muse's --help short-circuits whatever else is on the line, so it
        # cannot be handed the composed argv the way codex can.
        assert probe.strict is False

    def test_every_run_engine_has_a_probe(self):
        from engines import check as engine_check

        assert set(engine_run.RUN_ENGINES) <= set(engine_check.PROBES)


class TestA8sDefinitions:
    @pytest.mark.parametrize(
        "name", ["muse.json", "engine-muse.json", "engine-muse-unrestricted.json"]
    )
    def test_definition_exists_and_is_valid_json(self, name):
        data = json.loads((DEFINITIONS / name).read_text(encoding="utf-8"))
        assert data["description"]
        assert data["invoke"]

    def test_the_preset_points_at_a_definition_that_exists(self):
        named = HARNESS_PRESETS["muse"]["a8s_definition"]
        assert (DEFINITIONS / named).is_file()

    def test_direct_definition_invokes_muse_headlessly(self):
        data = json.loads((DEFINITIONS / "muse.json").read_text(encoding="utf-8"))
        assert data["invoke"][:2] == ["muse", "exec"]
        assert "--user-input-auto-resolve" in data["invoke"]

    def test_engine_definition_routes_through_r4t_engine_run(self):
        data = json.loads((DEFINITIONS / "engine-muse.json").read_text(encoding="utf-8"))
        for block in (data, data["batch"], data["idle"]):
            argv = block["invoke"]
            assert argv[2:5] == ["engine", "muse", "run"]

    def test_unrestricted_definition_passes_permissions_bypass(self):
        data = json.loads(
            (DEFINITIONS / "engine-muse-unrestricted.json").read_text(encoding="utf-8")
        )
        for block in (data, data["batch"], data["idle"]):
            argv = block["invoke"]
            i = argv.index("run")
            assert argv[i + 1:i + 3] == ["--permissions", "bypass"]
        assert "--yolo" in data["description"]
