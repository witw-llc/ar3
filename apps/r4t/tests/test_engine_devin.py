"""Devin is wired, not merely declared.

The suite's recurring defect is configuration that parses, validates and
documents cleanly with nothing behind it. These tests are the other half of
adding an engine: each one fails if a specific piece of the wiring is removed,
so `devin` cannot decay into a name the tables know and nothing honours.
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
    mcp_default,
    mcp_presets,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFINITIONS = REPO_ROOT / "apps" / "a8s" / "definitions"


def argv_for(**kwargs):
    base = dict(model=None, timeout=900, workdir=Path("/tmp"))
    base.update(kwargs)
    return engine_run.build_argv("devin", prompt="PROMPT", **base)


class TestPreset:
    def test_devin_is_a_preset_and_a_run_engine(self):
        assert "devin" in HARNESS_PRESETS
        assert "devin" in engine_run.RUN_ENGINES

    def test_capabilities_are_run_and_check_without_quota(self):
        # devin exposes no headless usage surface, so the module implements no
        # quota verb and the registry must not advertise one.
        assert engines.capabilities("devin") == ["run", "check"]
        assert engines.capability("devin", "quota") is None

    def test_quota_refuses_by_name_instead_of_raising_attributeerror(self):
        with pytest.raises(engines.QuotaError) as excinfo:
            engines.quota("devin")
        assert "no quota verb" in str(excinfo.value)

    def test_headless_invocation_is_print_mode_at_dangerous(self):
        assert argv_for() == [
            "devin",
            "--permission-mode", "dangerous",
            "--respect-workspace-trust", "false",
            "-p",
            "PROMPT",
        ]

    def test_model_is_spliced_after_the_binary(self):
        argv = argv_for(model="opus")
        assert argv[:3] == ["devin", "--model", "opus"]

    def test_continue_appends_dashdash_continue(self):
        argv = argv_for(continue_conversation=True)
        assert argv[-1] == "--continue"


class TestPermissionTranslation:
    def test_devin_is_registered_in_the_translation_table(self):
        assert PERMISSION_TRANSLATION["devin"].anchor is None

    def test_ask_is_below_the_floor(self):
        with pytest.raises(engine_run.RunError) as excinfo:
            argv_for(permissions="ask")
        assert "cannot run" in str(excinfo.value)
        assert "tell" in str(excinfo.value)

    def test_auto_is_below_the_floor(self):
        with pytest.raises(engine_run.RunError) as excinfo:
            argv_for(permissions="auto")
        assert "dangerous" in str(excinfo.value)

    def test_bypass_is_what_the_preset_already_carries(self):
        assert argv_for(permissions="bypass") == argv_for()


class TestRefusalsNameTheirReason:
    def test_allowed_tools_is_refused_with_a_reason(self):
        with pytest.raises(Exception) as excinfo:
            argv_for(allowed_tools="Read Write")
        assert "devin" in str(excinfo.value)
        assert "config.json" in allowed_tools_unsupported_reason("devin")

    def test_session_pin_is_refused(self):
        # devin can resume a known id (`-r`) but cannot found a session at a
        # caller-chosen one, so the pin is not offered.
        with pytest.raises(engine_run.RunError) as excinfo:
            argv_for(session="11111111-2222-3333-4444-555555555555")
        assert "no --session pin" in str(excinfo.value)


class TestMcpIdiom:
    def test_devin_has_the_file_idiom_and_it_is_opt_in(self):
        # `.devin/mcp_config.local.json` is written into the member's working
        # tree — a directory r4t does not own — so the knob defaults off.
        assert HARNESS_PRESETS["devin"]["mcp"] == "devin-file"
        assert "devin" in mcp_presets()
        assert mcp_default("devin") is False

    @pytest.mark.parametrize("seed", ["theirs.txt\n", "theirs.txt"])
    def test_devin_file_writes_the_gitignored_scope(self, tmp_path, seed):
        import subprocess

        import rig as rig_module

        # devin's own `mcp add --scope local` keeps the file invisible to
        # `git status` by appending it to .git/info/exclude; the writer has to
        # keep that promise without clobbering excludes already there — even
        # a file whose last line is unterminated.
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        exclude = tmp_path / ".git" / "info" / "exclude"
        exclude.write_text(seed, encoding="utf-8")

        path = rig_module._write_devin_mcp(
            tmp_path, {"TELL_OUTBOX_DIR": "/staging"}, ["python3", "a8s.py"]
        )
        assert path == tmp_path / ".devin" / "mcp_config.local.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        entry = payload["mcpServers"]["a8s"]
        assert entry["command"] == "python3"
        assert entry["env"]["TELL_OUTBOX_DIR"] == "/staging"

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert status.stdout == ""
        assert exclude.read_text(encoding="utf-8").splitlines()[-1] == (
            ".devin/mcp_config.local.json"
        )
        assert "theirs.txt" in exclude.read_text(encoding="utf-8").splitlines()

    def test_devin_file_preserves_existing_servers(self, tmp_path):
        import rig as rig_module

        existing = tmp_path / ".devin" / "mcp_config.local.json"
        existing.parent.mkdir(parents=True)
        existing.write_text(
            json.dumps({"mcpServers": {"theirs": {"command": "x"}}}),
            encoding="utf-8",
        )
        rig_module._write_devin_mcp(tmp_path, {}, ["python3", "a8s.py"])
        payload = json.loads(existing.read_text(encoding="utf-8"))
        assert set(payload["mcpServers"]) == {"theirs", "a8s"}


class TestCheckProbe:
    def test_a_run_engine_has_a_check_probe(self):
        from engines import check as engine_check

        assert "devin" in engine_check.PROBES
        probe = engine_check.PROBES["devin"]
        assert probe.help_binary == "devin"
        # devin's clap parser reports an unexpected argument even with --help
        # present (exit 2), so it can be handed the composed argv like codex.
        assert probe.strict is True


class TestA8sDefinitions:
    @pytest.mark.parametrize(
        "name", ["devin.json", "engine-devin.json", "engine-devin-unrestricted.json"]
    )
    def test_definition_exists_and_is_valid_json(self, name):
        data = json.loads((DEFINITIONS / name).read_text(encoding="utf-8"))
        assert data["description"]
        assert data["invoke"]

    def test_the_preset_points_at_a_definition_that_exists(self):
        named = HARNESS_PRESETS["devin"]["a8s_definition"]
        assert (DEFINITIONS / named).is_file()

    def test_direct_definition_invokes_devin_headlessly(self):
        data = json.loads((DEFINITIONS / "devin.json").read_text(encoding="utf-8"))
        assert data["invoke"][0] == "devin"
        assert "dangerous" in data["invoke"]
        assert "-p" in data["invoke"]

    def test_engine_definition_routes_through_r4t_engine_run(self):
        data = json.loads((DEFINITIONS / "engine-devin.json").read_text(encoding="utf-8"))
        for block in (data, data["batch"], data["idle"]):
            argv = block["invoke"]
            assert argv[2:5] == ["engine", "devin", "run"]

    def test_unrestricted_definition_passes_permissions_bypass(self):
        data = json.loads(
            (DEFINITIONS / "engine-devin-unrestricted.json").read_text(encoding="utf-8")
        )
        for block in (data, data["batch"], data["idle"]):
            argv = block["invoke"]
            i = argv.index("--permissions", argv.index("run"))
            assert argv[i + 1] == "bypass"
