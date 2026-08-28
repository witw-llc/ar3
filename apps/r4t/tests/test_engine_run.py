from __future__ import annotations
import re
from pathlib import Path as _P
REPO_ROOT = _P(__file__).resolve().parent.parent.parent.parent

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import engines
from engines import run as engine_run
from r4t import main as r4t_main
from rig import RigError


def fake_cli(tmp_path: Path, name: str = "fake-engine") -> Path:
    """A tiny stand-in CLI that records its own argv (one file per call) and
    exits 0 — no LLM, no network."""
    script = tmp_path / f"{name}.py"
    calls = tmp_path / f"{name}-calls"
    calls.mkdir(exist_ok=True)
    script.write_text(
        textwrap.dedent(
            f"""\
            import json, os, sys
            calls_dir = {str(calls)!r}
            n = len(os.listdir(calls_dir))
            with open(os.path.join(calls_dir, f"call-{{n:03d}}.json"), "w", encoding="utf-8", newline="") as f:
                json.dump(sys.argv[1:], f)
            print("fake engine ran")
            """
        ),
        encoding="utf-8",
    )
    return script, calls


class TestBuildArgv:
    def test_unsupported_engine_names_the_supported_set(self, tmp_path):
        # Bare `ollama` stays excluded: `ollama run` has no file tools, and
        # the scaffold's read/write contract needs them.
        with pytest.raises(engine_run.RunError, match="claude"):
            engine_run.build_argv("ollama", "hi", model=None, timeout=900, workdir=tmp_path)

    def test_claude_gets_model_flag_and_no_extras(self, tmp_path):
        argv = engine_run.build_argv(
            "claude", "do the thing", model="sonnet", timeout=900, workdir=tmp_path
        )
        assert argv[0] == "claude"
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "sonnet"
        assert argv[-1] == "do the thing"
        assert "--print-timeout" not in argv
        assert "--no-ask-user" not in argv

    def test_codex_model_flag_uses_dash_m(self, tmp_path):
        argv = engine_run.build_argv(
            "codex", "fix it", model="o4", timeout=900, workdir=tmp_path
        )
        assert "-m" in argv
        assert argv[argv.index("-m") + 1] == "o4"
        assert argv[-1] == "fix it"

    def test_cursor_model_flag(self, tmp_path):
        argv = engine_run.build_argv(
            "cursor", "go", model="opus", timeout=900, workdir=tmp_path
        )
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "opus"

    def test_copilot_gets_no_ask_user_and_rejects_model(self, tmp_path):
        argv = engine_run.build_argv(
            "copilot", "go", model=None, timeout=900, workdir=tmp_path
        )
        assert "--no-ask-user" in argv
        assert argv[-1] == "go"
        with pytest.raises(engine_run.RunError, match="--model"):
            engine_run.build_argv(
                "copilot", "go", model="anything", timeout=900, workdir=tmp_path
            )

    def test_copilot_does_not_double_no_ask_user_if_preset_already_carries_it(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setitem(
            engine_run.HARNESS_PRESETS,
            "copilot",
            {**engine_run.HARNESS_PRESETS["copilot"], "invoke": [
                "copilot", "--no-ask-user", "--allow-all-tools", "-p", "{prompt}",
            ]},
        )
        argv = engine_run.build_argv(
            "copilot", "go", model=None, timeout=900, workdir=tmp_path
        )
        assert argv.count("--no-ask-user") == 1

    def test_agy_always_gets_print_timeout_matching_the_run_timeout(self, tmp_path):
        argv = engine_run.build_argv(
            "agy", "go", model=None, timeout=45, workdir=tmp_path
        )
        assert "--print-timeout" in argv
        assert argv[argv.index("--print-timeout") + 1] == "45s"

    def test_agy_model_resolves_live_against_agy_models(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            engine_run, "resolve_agy_model", lambda query, **k: "Gemini 3.6 Flash Low"
        )
        argv = engine_run.build_argv(
            "agy", "go", model="flash", timeout=900, workdir=tmp_path
        )
        assert "Gemini 3.6 Flash Low" in argv
        assert "{model}" not in argv

    def test_agy_model_resolution_failure_is_a_run_error(self, monkeypatch, tmp_path):
        def boom(query, **k):
            raise RigError("no such model")

        monkeypatch.setattr(engine_run, "resolve_agy_model", boom)
        with pytest.raises(engine_run.RunError, match="no such model"):
            engine_run.build_argv(
                "agy", "go", model="nonsense", timeout=900, workdir=tmp_path
            )

    def test_opencode_substitutes_prompt_and_workdir(self, tmp_path):
        argv = engine_run.build_argv(
            "opencode", "do the thing", model=None, timeout=900, workdir=tmp_path
        )
        assert argv[0] == "opencode"
        assert argv[argv.index("--dir") + 1] == str(tmp_path)
        assert argv[-1] == "do the thing"

    def test_ollama_opencode_requires_model(self, tmp_path):
        with pytest.raises(engine_run.RunError, match="--model"):
            engine_run.build_argv(
                "ollama-opencode", "go", model=None, timeout=900, workdir=tmp_path
            )

    def test_ollama_opencode_materializes_model_and_workdir(self, tmp_path):
        argv = engine_run.build_argv(
            "ollama-opencode", "go", model="qwen3.6", timeout=900, workdir=tmp_path
        )
        assert argv[:5] == ["ollama", "launch", "opencode", "--model", "qwen3.6"]
        assert argv[argv.index("--dir") + 1] == str(tmp_path)
        assert argv[-1] == "go"

    def test_ollama_claude_materializes_model(self, tmp_path):
        argv = engine_run.build_argv(
            "ollama-claude", "go", model="qwen3.6", timeout=900, workdir=tmp_path
        )
        assert argv[:5] == ["ollama", "launch", "claude", "--model", "qwen3.6"]
        assert argv[-1] == "go"


def argv_for(engine, **kwargs):
    """Compose one engine's argv with the ollama launchers' required --model
    supplied, so a test can name an engine without restating that rule."""
    kwargs.setdefault("model", "qwen3.6" if engine.startswith("ollama-") else None)
    kwargs.setdefault("timeout", 900)
    kwargs.setdefault("workdir", Path("/w"))
    return engine_run.build_argv(engine, "P", **kwargs)


class TestContinueFlag:
    CAN_CONTINUE = ["claude", "codex", "cursor", "opencode", "agy", "ollama-opencode"]
    CANNOT = ["copilot", "ollama-claude", "ollama-codex"]

    @pytest.mark.parametrize("engine", CAN_CONTINUE)
    def test_continuing_engines_gain_their_own_tokens(self, engine):
        plain = argv_for(engine)
        argv = argv_for(engine, continue_conversation=True)
        assert argv != plain
        assert "P" in argv  # the prompt survives the splice

    def test_flag_shaped_clis_append_at_the_end(self):
        assert argv_for("claude", continue_conversation=True)[-2:] == ["P", "--continue"]
        assert argv_for("agy", continue_conversation=True)[-1] == "--continue"

    def test_codex_anchors_resume_after_exec_and_drops_the_sandbox_flag(self):
        # `codex exec resume` is its own clap subcommand and rejects
        # -s/--sandbox outright (verified against codex-cli 0.147.0), so the
        # pair comes out rather than composing an argv the CLI refuses.
        argv = argv_for("codex", continue_conversation=True)
        assert argv[:5] == [
            "codex", "exec", "resume", "--last", "--include-non-interactive",
        ]
        assert "--sandbox" not in argv
        assert "workspace-write" not in argv
        assert "--skip-git-repo-check" in argv

    def test_codex_bypass_survives_continuation(self):
        argv = argv_for("codex", continue_conversation=True, permissions="bypass")
        assert argv[:5] == [
            "codex", "exec", "resume", "--last", "--include-non-interactive",
        ]
        assert "--dangerously-bypass-approvals-and-sandbox" in argv

    def test_opencode_keeps_its_workdir_and_gains_continue(self):
        argv = argv_for("opencode", continue_conversation=True)
        assert argv[argv.index("--dir") + 1] == "/w"
        assert "--continue" in argv

    @pytest.mark.parametrize("engine", CANNOT)
    def test_unsupported_engine_errors_naming_the_engines_that_can(self, engine):
        with pytest.raises(engine_run.RunError) as exc:
            argv_for(engine, continue_conversation=True)
        message = str(exc.value)
        assert "cannot continue" in message
        for able in ["claude", "codex", "cursor", "opencode"]:
            assert able in message

    def test_copilot_refusal_says_why(self):
        # A user who sees `--continue` in `copilot --help` needs to know r4t
        # refuses it on purpose, not that r4t is broken.
        with pytest.raises(engine_run.RunError) as exc:
            argv_for("copilot", continue_conversation=True)
        assert "machine's most recent session" in str(exc.value)
        assert "#17" in str(exc.value)

    def test_anchor_missing_from_a_handedited_invoke_fails_closed(self, monkeypatch):
        monkeypatch.setitem(
            engine_run.HARNESS_PRESETS, "codex",
            {**engine_run.HARNESS_PRESETS["codex"],
             "invoke": ["codex", "--sandbox", "workspace-write", "{prompt}"]},
        )
        with pytest.raises(engine_run.RunError, match="cannot continue"):
            argv_for("codex", continue_conversation=True)


class TestPermissionsFlag:
    def test_unset_is_byte_identical_to_the_preset(self):
        for engine in sorted(engine_run.RUN_ENGINES):
            assert argv_for(engine) == argv_for(engine, permissions=None)

    def test_auto_is_where_the_presets_already_sit(self):
        for engine in ["claude", "codex", "cursor", "opencode", "copilot"]:
            assert argv_for(engine, permissions="auto") == argv_for(engine)

    def test_claude_ask_drops_both_permission_flags(self):
        argv = argv_for("claude", permissions="ask")
        assert "--permission-mode" not in argv
        assert "--allowedTools" not in argv
        assert "dontAsk" not in argv
        assert argv[-2:] == ["-p", "P"]

    def test_claude_bypass_swaps_the_mode_and_keeps_the_allowlist(self):
        argv = argv_for("claude", permissions="bypass")
        assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
        assert "--allowedTools" in argv

    def test_cursor_ask_drops_the_auto_approval_tokens(self):
        argv = argv_for("cursor", permissions="ask")
        for token in ("--trust", "--force", "--approve-mcps"):
            assert token not in argv
        assert argv[-1] == "P"

    def test_opencode_ask_drops_auto(self):
        argv = argv_for("opencode", permissions="ask")
        assert "--auto" not in argv
        assert argv[argv.index("--dir") + 1] == "/w"

    def test_codex_bypass_replaces_the_sandbox_flag(self):
        argv = argv_for("codex", permissions="bypass")
        assert "--dangerously-bypass-approvals-and-sandbox" in argv
        assert "--sandbox" not in argv
        assert "workspace-write" not in argv

    def test_copilot_bypass_widens_allow_all_tools(self):
        argv = argv_for("copilot", permissions="bypass")
        assert "--allow-all" in argv
        assert "--allow-all-tools" not in argv

    def test_ollama_launchers_translate_the_wrapped_engine(self):
        argv = argv_for("ollama-claude", permissions="bypass")
        assert argv[:3] == ["ollama", "launch", "claude"]
        assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
        assert argv_for("ollama-opencode", permissions="ask").count("--auto") == 0
        assert "--dangerously-bypass-approvals-and-sandbox" in argv_for(
            "ollama-codex", permissions="bypass"
        )

    @pytest.mark.parametrize(
        "engine,mode,reason",
        [
            ("codex", "ask", "approval policy"),
            ("ollama-codex", "ask", "approval policy"),
            ("copilot", "ask", "--allow-all-tools"),
            ("agy", "ask", "auto-denies"),
            ("agy", "auto", "auto-denies"),
        ],
    )
    def test_below_the_floor_is_a_hard_error_with_the_reason(self, engine, mode, reason):
        with pytest.raises(engine_run.RunError) as exc:
            argv_for(engine, permissions=mode)
        assert reason in str(exc.value)
        assert mode in str(exc.value)

    @pytest.mark.parametrize("engine", ["cursor", "opencode", "ollama-opencode"])
    def test_above_the_ceiling_accepts_and_equals_auto(self, engine):
        # Requesting a stronger mode than the engine offers is not a safety
        # failure — the argv is the most permissive one available.
        assert argv_for(engine, permissions="bypass") == argv_for(engine, permissions="auto")

    @pytest.mark.parametrize("engine", ["cursor", "opencode"])
    def test_the_ceiling_note_names_the_engine_and_its_strongest_mode(self, engine):
        _, note = engine_run._build_argv_template(
            engine, model=None, timeout=900, workdir=Path("/w"), permissions="bypass"
        )
        assert note == f"{engine}'s strongest mode is 'auto'; 'bypass' means the same here"

    def test_codex_bypass_note_says_the_sandbox_goes_too(self):
        _, note = engine_run._build_argv_template(
            "codex", model=None, timeout=900, workdir=Path("/w"), permissions="bypass"
        )
        assert "drops the sandbox" in note

    def test_no_note_when_the_mode_is_unset(self):
        _, note = engine_run._build_argv_template(
            "claude", model=None, timeout=900, workdir=Path("/w")
        )
        assert note is None

    def test_unknown_mode_is_rejected(self):
        with pytest.raises(engine_run.RunError, match="unknown permissions mode"):
            argv_for("claude", permissions="yolo")


class TestAllowedToolsFlag:
    def test_claude_replaces_the_presets_list(self):
        argv = argv_for("claude", allowed_tools="Bash(git:*) Read")
        assert argv[argv.index("--allowedTools") + 1] == "Bash(git:*) Read"
        assert "TodoWrite" not in " ".join(argv)

    def test_ollama_claude_replaces_it_too(self):
        argv = argv_for("ollama-claude", allowed_tools="Read")
        assert argv[argv.index("--allowedTools") + 1] == "Read"

    def test_ask_then_an_explicit_list_re_adds_the_flag(self):
        argv = argv_for("claude", permissions="ask", allowed_tools="Read")
        assert "--permission-mode" not in argv
        assert argv[argv.index("--allowedTools") + 1] == "Read"

    @pytest.mark.parametrize(
        "engine,reason",
        [
            ("codex", "config.toml"),
            ("cursor", "cli-config.json"),
            ("opencode", "opencode.json"),
            ("agy", "settings.json"),
            ("copilot", "--allow-tool"),
        ],
    )
    def test_unsupported_engines_error_with_the_reason(self, engine, reason):
        with pytest.raises(engine_run.RunError) as exc:
            argv_for(engine, allowed_tools="Read")
        assert reason in str(exc.value)
        assert "claude" in str(exc.value)  # names the engines that can


class TestScaffold:
    def test_byte_stable_across_two_invocations_same_dir(self, tmp_path):
        first = engine_run.scaffold_prompt(tmp_path, "do the thing", agent="bob")
        second = engine_run.scaffold_prompt(tmp_path, "do the thing", agent="bob")
        assert first == second

    def test_only_the_routed_input_varies(self, tmp_path):
        first = engine_run.scaffold_prompt(tmp_path, "message one", agent="bob")
        second = engine_run.scaffold_prompt(tmp_path, "message two", agent="bob")
        prelude_a, _, _ = first.partition("Routed input:\n")
        prelude_b, _, _ = second.partition("Routed input:\n")
        assert prelude_a == prelude_b
        assert first.endswith("message one")
        assert second.endswith("message two")

    def test_no_agent_omits_the_convo_step_and_renumbers(self, tmp_path):
        text = engine_run.scaffold_prompt(tmp_path, "hi", agent=None)
        assert "a8s convo" not in text
        assert "1. Read" in text
        assert "2. Stay idle" in text
        assert "3. Before exit" in text
        assert "4." not in text.split("Routed input:")[0]

    def test_agent_adds_the_convo_step(self, tmp_path):
        text = engine_run.scaffold_prompt(tmp_path, "hi", agent="my-node")
        assert "2. Run `a8s convo my-node`" in text
        assert "3. Stay idle" in text
        assert "4. Before exit" in text

    def test_paths_are_absolute_and_under_dir(self, tmp_path):
        text = engine_run.scaffold_prompt(tmp_path, "hi", agent=None)
        assert str(tmp_path / "STATUS.md") in text
        assert str(tmp_path / "AGENTS.md") in text
        assert str(tmp_path / "LESSONS.md") in text


class TestLessonsRotation:
    def test_short_lessons_is_silent(self, tmp_path, capsys):
        (tmp_path / "LESSONS.md").write_text("- one\n- two\n", encoding="utf-8")
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=200)
        assert capsys.readouterr().err == ""
        assert not (tmp_path / engine_run.LESSONS_ARCHIVE_NAME).exists()

    def test_no_op_at_exactly_cap(self, tmp_path, capsys):
        lessons = tmp_path / "LESSONS.md"
        original = "\n".join(f"- lesson {i}" for i in range(200)) + "\n"
        lessons.write_text(original, encoding="utf-8")
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=200)
        assert capsys.readouterr().err == ""
        assert lessons.read_text(encoding="utf-8") == original
        assert not (tmp_path / engine_run.LESSONS_ARCHIVE_NAME).exists()

    def test_rotation_triggers_at_cap_plus_one_and_lands_at_exactly_cap(
        self, tmp_path, capsys
    ):
        lessons = tmp_path / "LESSONS.md"
        lessons.write_text(
            "\n".join(f"- lesson {i}" for i in range(201)) + "\n", encoding="utf-8"
        )
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=200)
        kept = lessons.read_text(encoding="utf-8").splitlines()
        assert len(kept) == 200
        assert kept[0] == "- lesson 1"  # oldest (lesson 0) moved out
        assert kept[-1] == "- lesson 200"
        archive = tmp_path / engine_run.LESSONS_ARCHIVE_NAME
        assert archive.read_text(encoding="utf-8") == "- lesson 0\n"

    def test_stderr_rotation_notice_format(self, tmp_path, capsys):
        lessons = tmp_path / "LESSONS.md"
        lessons.write_text(
            "\n".join(f"- lesson {i}" for i in range(205)) + "\n", encoding="utf-8"
        )
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=200)
        err = capsys.readouterr().err
        archive = tmp_path / engine_run.LESSONS_ARCHIVE_NAME
        assert err == (
            f"r4t engine: rotated 5 lines from {lessons} to {archive}\n"
        )

    def test_archive_receives_lines_in_order_across_successive_rotations(
        self, tmp_path, capsys
    ):
        lessons = tmp_path / "LESSONS.md"
        archive = tmp_path / engine_run.LESSONS_ARCHIVE_NAME
        lessons.write_text(
            "\n".join(f"- lesson {i}" for i in range(203)) + "\n", encoding="utf-8"
        )
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=200)
        assert archive.read_text(encoding="utf-8") == (
            "- lesson 0\n- lesson 1\n- lesson 2\n"
        )
        capsys.readouterr()

        for i in range(203, 206):
            with lessons.open("a", encoding="utf-8") as f:
                f.write(f"- lesson {i}\n")
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=200)
        assert archive.read_text(encoding="utf-8") == (
            "- lesson 0\n- lesson 1\n- lesson 2\n"
            "- lesson 3\n- lesson 4\n- lesson 5\n"
        )
        assert lessons.read_text(encoding="utf-8").splitlines()[0] == "- lesson 6"

    def test_missing_lessons_is_silent(self, tmp_path, capsys):
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=200)
        assert capsys.readouterr().err == ""
        assert not (tmp_path / engine_run.LESSONS_ARCHIVE_NAME).exists()


class TestExecuteAndSpawn:
    def test_no_scaffold_passes_prompt_untouched(self, tmp_path):
        script, calls = fake_cli(tmp_path)
        argv_template = [sys.executable, str(script), "{prompt}"]
        # Patch a fake preset in so `execute` can be exercised end to end
        # without needing a real engine CLI on the test machine.
        import rig as rig_module

        original = dict(rig_module.HARNESS_PRESETS.get("claude", {}))
        rig_module.HARNESS_PRESETS["claude"] = {**original, "invoke": argv_template}
        try:
            code = engine_run.execute(
                "claude", "raw prompt text",
                dir_path=tmp_path, model=None, agent=None, timeout=30,
                scaffold=False,
            )
        finally:
            rig_module.HARNESS_PRESETS["claude"] = original
        assert code == 0
        [call] = sorted(calls.iterdir())
        import json as jsonlib
        recorded = jsonlib.loads(call.read_text())
        assert recorded == ["raw prompt text"]

    def test_scaffold_on_wraps_the_prompt(self, tmp_path):
        script, calls = fake_cli(tmp_path)
        import rig as rig_module

        original = dict(rig_module.HARNESS_PRESETS.get("claude", {}))
        rig_module.HARNESS_PRESETS["claude"] = {
            **original, "invoke": [sys.executable, str(script), "{prompt}"],
        }
        try:
            code = engine_run.execute(
                "claude", "raw prompt text",
                dir_path=tmp_path, model=None, agent=None, timeout=30,
                scaffold=True,
            )
        finally:
            rig_module.HARNESS_PRESETS["claude"] = original
        assert code == 0
        [call] = sorted(calls.iterdir())
        import json as jsonlib
        recorded = jsonlib.loads(call.read_text())
        assert recorded[0].startswith("Smart cold boot:")
        assert recorded[0].endswith("raw prompt text")

    def test_timeout_kills_the_process_group(self, tmp_path):
        script = tmp_path / "sleepy.py"
        script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        import rig as rig_module

        original = dict(rig_module.HARNESS_PRESETS.get("claude", {}))
        rig_module.HARNESS_PRESETS["claude"] = {
            **original, "invoke": [sys.executable, str(script), "{prompt}"],
        }
        try:
            code = engine_run.execute(
                "claude", "x",
                dir_path=tmp_path, model=None, agent=None, timeout=1,
                scaffold=False,
            )
        finally:
            rig_module.HARNESS_PRESETS["claude"] = original
        assert code == engine_run.TIMEOUT_EXIT_CODE

    def test_spawn_failure_is_a_run_error(self, tmp_path):
        with pytest.raises(engine_run.RunError, match="failed to spawn"):
            engine_run._spawn(["/no/such/binary-r4t-engine"], tmp_path, 5)

    def test_echo_writes_argv_and_prompt_to_stderr(self, tmp_path, capsys):
        script, calls = fake_cli(tmp_path)
        import rig as rig_module

        original = dict(rig_module.HARNESS_PRESETS.get("claude", {}))
        rig_module.HARNESS_PRESETS["claude"] = {
            **original, "invoke": [sys.executable, str(script), "{prompt}"],
        }
        try:
            code = engine_run.execute(
                "claude", "raw prompt text",
                dir_path=tmp_path, model=None, agent=None, timeout=30,
                scaffold=False, echo=True,
            )
        finally:
            rig_module.HARNESS_PRESETS["claude"] = original
        assert code == 0
        [call] = sorted(calls.iterdir())  # the turn still ran
        err = capsys.readouterr().err
        assert sys.executable in err
        assert str(script) in err
        assert "raw prompt text" in err
        argv_line = next(
            line for line in err.splitlines() if line.startswith("r4t engine echo: argv:")
        )
        # The argv line keeps the literal placeholder — it is never
        # value-matched against the prompt string — and the prompt itself
        # appears only in the prompt block below it.
        assert argv_line.count("{prompt}") == 1
        assert "raw prompt text" not in argv_line

    def test_print_echo_does_not_elide_argv_elements_equal_to_the_prompt(self, capsys):
        # Regression: `_print_echo` used to replace every argv element equal
        # to the prompt string, so a prompt identical to the executable name
        # (e.g. "claude") elided the executable itself, not just the prompt
        # slot. The fix threads the `{prompt}` placeholder through unchanged
        # instead of value-matching.
        template = ["claude", "--flag", "{prompt}"]
        engine_run._print_echo(template, "claude")
        err = capsys.readouterr().err
        argv_line = next(
            line for line in err.splitlines() if line.startswith("r4t engine echo: argv:")
        )
        assert argv_line == "r4t engine echo: argv: claude --flag '{prompt}'"
        assert argv_line.count("{prompt}") == 1

    def test_no_echo_by_default_is_silent_on_stderr(self, tmp_path, capsys):
        script, calls = fake_cli(tmp_path)
        import rig as rig_module

        original = dict(rig_module.HARNESS_PRESETS.get("claude", {}))
        rig_module.HARNESS_PRESETS["claude"] = {
            **original, "invoke": [sys.executable, str(script), "{prompt}"],
        }
        try:
            code = engine_run.execute(
                "claude", "raw prompt text",
                dir_path=tmp_path, model=None, agent=None, timeout=30,
                scaffold=False,
            )
        finally:
            rig_module.HARNESS_PRESETS["claude"] = original
        assert code == 0
        assert capsys.readouterr().err == ""


class TestCapabilities:
    def test_run_engines_report_every_verb(self):
        for name in engine_run.RUN_ENGINES:
            assert engines.capabilities(name) == ["quota", "run", "check"]

    def test_non_run_engines_report_only_quota(self):
        for name in engines.MODULES:
            if name not in engine_run.RUN_ENGINES:
                assert engines.capabilities(name) == ["quota"]

    def test_run_supported_through_a_preset_id(self):
        assert "run" in engines.capabilities("claude")


def engine_cli(*args):
    return r4t_main(["engine", *args])


class TestEngineRunCli:
    def test_unsupported_engine_errors_clearly(self, capsys):
        # Bare `ollama` stays excluded: `ollama run` has no file tools.
        assert engine_cli("ollama", "run", "hi") == 1
        err = capsys.readouterr().err
        assert "does not support run" in err
        for name in sorted(engine_run.RUN_ENGINES):
            assert name in err

    def test_prompt_required_without_idle(self, capsys, tmp_path):
        assert engine_cli("claude", "run", "--dir", str(tmp_path)) == 2
        assert "PROMPT is required" in capsys.readouterr().err

    def test_no_scaffold_end_to_end(self, tmp_path, monkeypatch):
        script, calls = fake_cli(tmp_path)
        import rig as rig_module

        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "claude",
            {**rig_module.HARNESS_PRESETS["claude"],
             "invoke": [sys.executable, str(script), "{prompt}"]},
        )
        code = engine_cli(
            "claude", "run", "--dir", str(tmp_path), "--no-scaffold", "raw text",
        )
        assert code == 0
        [call] = sorted(calls.iterdir())
        import json as jsonlib
        assert jsonlib.loads(call.read_text()) == ["raw text"]

    def test_idle_second_run_exits_zero_without_invoking(self, tmp_path, monkeypatch):
        script, calls = fake_cli(tmp_path)
        import rig as rig_module

        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "claude",
            {**rig_module.HARNESS_PRESETS["claude"],
             "invoke": [sys.executable, str(script), "{prompt}"]},
        )
        first = engine_cli("claude", "run", "--dir", str(tmp_path), "--idle")
        assert first == 0
        assert len(list(calls.iterdir())) == 1
        assert (tmp_path / ".engine-idle").exists()

        second = engine_cli("claude", "run", "--dir", str(tmp_path), "--idle")
        assert second == 0
        assert len(list(calls.iterdir())) == 1  # no second invocation

    def test_non_idle_run_clears_the_latch(self, tmp_path, monkeypatch):
        script, calls = fake_cli(tmp_path)
        import rig as rig_module

        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "claude",
            {**rig_module.HARNESS_PRESETS["claude"],
             "invoke": [sys.executable, str(script), "{prompt}"]},
        )
        (tmp_path / ".engine-idle").touch()
        code = engine_cli("claude", "run", "--dir", str(tmp_path), "real work")
        assert code == 0
        assert not (tmp_path / ".engine-idle").exists()
        assert len(list(calls.iterdir())) == 1

    def test_idle_without_prompt_uses_the_builtin_idle_prompt(self, tmp_path, monkeypatch):
        script, calls = fake_cli(tmp_path)
        import rig as rig_module

        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "claude",
            {**rig_module.HARNESS_PRESETS["claude"],
             "invoke": [sys.executable, str(script), "{prompt}"]},
        )
        code = engine_cli("claude", "run", "--dir", str(tmp_path), "--idle", "--no-scaffold")
        assert code == 0
        [call] = sorted(calls.iterdir())
        import json as jsonlib
        assert jsonlib.loads(call.read_text()) == [engine_run.DEFAULT_IDLE_PROMPT]

    def test_stdin_dash_reads_the_prompt(self, tmp_path, monkeypatch):
        script, calls = fake_cli(tmp_path)
        import rig as rig_module

        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "claude",
            {**rig_module.HARNESS_PRESETS["claude"],
             "invoke": [sys.executable, str(script), "{prompt}"]},
        )
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("from stdin"))
        code = engine_cli("claude", "run", "--dir", str(tmp_path), "--no-scaffold", "-")
        assert code == 0
        [call] = sorted(calls.iterdir())
        import json as jsonlib
        assert jsonlib.loads(call.read_text()) == ["from stdin"]

    def test_lessons_cap_flag_reaches_execute(self, tmp_path, monkeypatch, capsys):
        script, calls = fake_cli(tmp_path)
        import rig as rig_module

        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "claude",
            {**rig_module.HARNESS_PRESETS["claude"],
             "invoke": [sys.executable, str(script), "{prompt}"]},
        )
        lessons = tmp_path / "LESSONS.md"
        lessons.write_text(
            "\n".join(f"- lesson {i}" for i in range(6)) + "\n", encoding="utf-8"
        )
        code = engine_cli(
            "claude", "run", "--dir", str(tmp_path), "--lessons-cap", "5", "do work",
        )
        assert code == 0
        assert len(list(calls.iterdir())) == 1  # the turn still ran
        archive = tmp_path / engine_run.LESSONS_ARCHIVE_NAME
        err = capsys.readouterr().err
        assert err == f"r4t engine: rotated 1 lines from {lessons} to {archive}\n"
        assert lessons.read_text(encoding="utf-8").splitlines() == [
            f"- lesson {i}" for i in range(1, 6)
        ]

    @pytest.mark.parametrize("bad_value", ["-1", "0"])
    def test_lessons_cap_rejects_non_positive_values(
        self, tmp_path, monkeypatch, capsys, bad_value
    ):
        # A negative cap archives every line and a zero cap empties
        # LESSONS.md on every turn — neither is meaningful, so argparse
        # itself should refuse before `execute` (and thus rotation) ever
        # runs.
        script, calls = fake_cli(tmp_path)
        import rig as rig_module

        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "claude",
            {**rig_module.HARNESS_PRESETS["claude"],
             "invoke": [sys.executable, str(script), "{prompt}"]},
        )
        lessons = tmp_path / "LESSONS.md"
        lessons.write_text("- lesson 0\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            engine_cli(
                "claude", "run", "--dir", str(tmp_path),
                "--lessons-cap", bad_value, "do work",
            )
        assert exc_info.value.code == 2
        assert "must be a positive integer" in capsys.readouterr().err
        assert list(calls.iterdir()) == []  # the engine CLI never ran
        assert lessons.read_text(encoding="utf-8") == "- lesson 0\n"

    def test_lessons_cap_positive_value_still_passes_through(self, tmp_path, monkeypatch):
        script, calls = fake_cli(tmp_path)
        import rig as rig_module

        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "claude",
            {**rig_module.HARNESS_PRESETS["claude"],
             "invoke": [sys.executable, str(script), "{prompt}"]},
        )
        code = engine_cli(
            "claude", "run", "--dir", str(tmp_path), "--lessons-cap", "3", "do work",
        )
        assert code == 0
        assert len(list(calls.iterdir())) == 1


class TestEngineRunFlagsCli:
    def test_idle_and_continue_contradict(self, tmp_path, capsys):
        # #155 rule 4, enforced mechanically: an idle wake is a cold start.
        assert engine_cli("claude", "run", "--dir", str(tmp_path), "--idle", "--continue") == 2
        assert "idle" in capsys.readouterr().err
        assert not (tmp_path / ".engine-idle").exists()  # the latch never armed

    def test_continue_on_an_engine_that_cannot_exits_one(self, tmp_path, capsys):
        assert engine_cli("copilot", "run", "--dir", str(tmp_path), "--continue", "go") == 1
        assert "#17" in capsys.readouterr().err

    def test_permissions_below_the_floor_exits_one(self, tmp_path, capsys):
        assert engine_cli(
            "agy", "run", "--dir", str(tmp_path), "--permissions", "auto", "go"
        ) == 1
        assert "auto-denies" in capsys.readouterr().err

    def test_unknown_permissions_mode_is_refused_by_the_parser(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc:
            engine_cli("claude", "run", "--dir", str(tmp_path), "--permissions", "yolo", "go")
        assert exc.value.code == 2
        assert "yolo" in capsys.readouterr().err

    def test_echo_prints_the_final_composed_argv(self, tmp_path, monkeypatch, capsys):
        # The composed argv is the whole diagnostic: a translation the caller
        # cannot see is a translation the caller cannot debug.
        code = engine_cli(
            "claude", "run", "--dir", str(tmp_path), "--no-scaffold", "--echo",
            "--permissions", "bypass", "--allowed-tools", "Read Edit", "go",
        )
        argv_line = next(
            line for line in capsys.readouterr().err.splitlines()
            if line.startswith("r4t engine echo: argv:")
        )
        assert "--permission-mode bypassPermissions" in argv_line
        assert "'Read Edit'" in argv_line
        assert "dontAsk" not in argv_line
        # claude is not installed in CI; the composition is what is asserted.
        assert code in (0, 1, 127)

    def test_bypass_note_reaches_stderr_before_the_turn(self, tmp_path, monkeypatch):
        import rig as rig_module

        script, calls = fake_cli(tmp_path)
        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "opencode",
            {**rig_module.HARNESS_PRESETS["opencode"],
             "invoke": [sys.executable, str(script), "run", "--auto", "{prompt}"]},
        )
        import io
        err = io.StringIO()
        monkeypatch.setattr(sys, "stderr", err)
        code = engine_cli(
            "opencode", "run", "--dir", str(tmp_path), "--no-scaffold",
            "--permissions", "bypass", "go",
        )
        assert code == 0
        assert len(list(calls.iterdir())) == 1  # the turn still ran
        assert "opencode's strongest mode is 'auto'" in err.getvalue()

    def test_continue_reaches_the_engine(self, tmp_path, monkeypatch):
        import rig as rig_module

        script, calls = fake_cli(tmp_path)
        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "claude",
            {**rig_module.HARNESS_PRESETS["claude"],
             "invoke": [sys.executable, str(script), "{prompt}"]},
        )
        code = engine_cli(
            "claude", "run", "--dir", str(tmp_path), "--no-scaffold", "--continue", "go",
        )
        assert code == 0
        [call] = sorted(calls.iterdir())
        import json as jsonlib
        assert jsonlib.loads(call.read_text()) == ["go", "--continue"]


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
class TestRelocatedFallbackTeardown:
    """The relocated-copy fallbacks (ark unimportable) must carry the same
    capture-pgid-before-SIGTERM behavior as ark.proc: a leader that has
    already been reaped when SIGKILL fires must not strand a SIGTERM-ignoring
    grandchild in the still-live process group."""

    SCENARIO = """
import importlib.abc, os, pathlib, signal, sys, time


class BlockArk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "ark" or fullname.startswith("ark."):
            raise ImportError("ark blocked (relocated-copy simulation)")
        return None


sys.meta_path.insert(0, BlockArk())
sys.path.insert(0, {r4t_dir!r})

{import_and_kill}

pidfile = pathlib.Path({pidfile!r})
child_script = pathlib.Path({child_script!r})
import subprocess
leader = subprocess.Popen(
    ["/bin/sh", "-c", f"{{sys.executable}} {{child_script}} {{pidfile}} & exit 0"],
    stdin=subprocess.DEVNULL,
    start_new_session=True,
)
for _ in range(200):
    if pidfile.exists() and pidfile.read_text().strip():
        break
    time.sleep(0.05)
child_pid = int(pidfile.read_text())
leader.wait()  # leader reaped: getpgid(leader.pid) now fails everywhere
kill(leader, grace_seconds=0.3)
time.sleep(0.2)
try:
    os.kill(child_pid, 0)
    print("ALIVE")
except ProcessLookupError:
    print("DEAD")
"""

    CHILD = (
        "import os, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "open(sys.argv[1], 'w', encoding='utf-8', newline='').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )

    @pytest.mark.parametrize(
        "import_and_kill",
        [
            (
                "from engines import run as m\n"
                "assert m._terminate_group.__module__ == 'engines.run'\n"
                "kill = m._terminate_group"
            ),
            (
                "import sandbox as m\n"
                "assert m._terminate_group.__module__ == 'sandbox'\n"
                "def kill(proc, *, grace_seconds):\n"
                "    m._terminate_group(proc.pid, grace_seconds=grace_seconds)"
            ),
        ],
        ids=["engines.run-fallback", "sandbox-fallback"],
    )
    def test_fallback_kills_grandchild_after_leader_is_reaped(
        self, tmp_path, import_and_kill
    ):
        child_script = tmp_path / "child.py"
        child_script.write_text(self.CHILD, encoding="utf-8")
        scenario = tmp_path / "scenario.py"
        r4t_dir = str(Path(__file__).resolve().parent.parent)
        scenario.write_text(
            self.SCENARIO.format(
                r4t_dir=r4t_dir,
                import_and_kill=import_and_kill,
                pidfile=str(tmp_path / "child.pid"),
                child_script=str(child_script),
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(scenario)],
            capture_output=True, text=True, timeout=60, cwd=str(tmp_path),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "DEAD", result.stdout + result.stderr


class TestArgv0IsResolvedBeforeExec:
    """Windows' CreateProcess appends only `.exe` to a bare name, never `.cmd`.
    Every npm global install is a `.cmd` shim — codex, opencode and cursor all
    arrive that way — so `shutil.which` finds the CLI and the exec then fails
    with WinError 2. The Windows seat measured both halves against the real
    `codex`: `which` returned `codex.CMD`, `subprocess.run(['codex', ...])`
    raised WinError 2, and the same call with the resolved path succeeded.
    """

    def test_a_bare_name_becomes_the_path_it_runs_from(self, tmp_path, monkeypatch):
        target = tmp_path / "codex.CMD"
        target.write_text("", encoding="utf-8")
        monkeypatch.setattr(
            engine_run.shutil, "which",
            lambda name: str(target) if name == "codex" else None,
        )
        assert engine_run.resolve_argv0(["codex", "--version"]) == [
            str(target), "--version"
        ]

    def test_a_path_is_left_alone(self, tmp_path, monkeypatch):
        def _boom(_name):  # resolution must not even be attempted
            raise AssertionError("a path should not be resolved again")

        monkeypatch.setattr(engine_run.shutil, "which", _boom)
        given = str(tmp_path / "codex")
        assert engine_run.resolve_argv0([given, "--version"]) == [given, "--version"]

    def test_a_name_that_resolves_to_nothing_is_left_for_the_os_to_reject(
        self, monkeypatch
    ):
        """Substituting None would hand subprocess a nonsense argv and lose the
        OS's own error, which is the one the operator needs."""
        monkeypatch.setattr(engine_run.shutil, "which", lambda _name: None)
        assert engine_run.resolve_argv0(["nope", "-x"]) == ["nope", "-x"]

    def test_an_empty_argv_is_not_indexed(self, monkeypatch):
        monkeypatch.setattr(engine_run.shutil, "which", lambda _name: None)
        assert engine_run.resolve_argv0([]) == []

    def test_the_spawn_path_execs_the_resolved_program(self, tmp_path, monkeypatch):
        """The composed argv keeps the readable name; only what reaches the OS
        is resolved."""
        seen: list[list[str]] = []

        class _Proc:
            returncode = 0

            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr(
            engine_run, "_proc_spawn",
            lambda argv, cwd, env=None: (seen.append(list(argv)), _Proc())[1],
        )
        monkeypatch.setattr(
            engine_run.shutil, "which",
            lambda name: f"/resolved/{name}.CMD" if name == "codex" else None,
        )
        engine_run._spawn(["codex", "--version"], tmp_path, 5)
        assert seen == [["/resolved/codex.CMD", "--version"]]


class TestNoUserFacingStringNamesAToolOutsideTheSuite:
    """A note that tells the reader to run something they do not have is worse
    than no note. The owner hit one in `engine agy quota`: it pointed at a
    private tool on his own machine, which no user of this repo can install.

    Scoped to r4t, where the defect was. `tools/no-private-tools.py` is the
    repo-wide one and runs at release, because a scan of every app cannot live
    in one app's path-filtered suite.
    """

    # r4t's own sources only. A repo-wide scan living in one app's suite is
    # green whenever the workflow routes elsewhere — the same hole that let a
    # shim guard sleep through a shim change. The repo-wide version is
    # `tools/no-private-tools.py`, which `release.yml` runs over the whole tree.
    FORBIDDEN = re.compile(r"\b(n0b)\b|~/bin/|\$HOME/bin/")
    SOURCES = sorted(
        path
        for path in (REPO_ROOT / "apps" / "r4t").rglob("*.py")
        if "tests" not in path.parts and "_vendor" not in path.parts
    )

    def test_the_scan_found_sources(self):
        assert len(self.SOURCES) > 5, len(self.SOURCES)

    def test_no_source_names_a_private_tool(self):
        offenders = []
        for path in self.SOURCES:
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if self.FORBIDDEN.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{n}: {line.strip()}")
        assert not offenders, (
            "a shipped source names a tool the reader does not have:\n"
            + "\n".join(offenders)
        )
