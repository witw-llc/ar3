from __future__ import annotations
import re
from pathlib import Path as _P
REPO_ROOT = _P(__file__).resolve().parent.parent.parent.parent

import os
import shutil
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

    def test_cursor_defaults_model_to_composer_when_unset(self, tmp_path):
        # `a8s vars <name> set MODEL ...` is optional, so an idle engine-cursor
        # node runs this with model=None — the same unpinned turn that used to
        # spend on the CLI's own `auto` (#282). It rides the cursor preset's
        # `model_default`, the same fix `r4t rig add cursor` already gets.
        argv = engine_run.build_argv(
            "cursor", "go", model=None, timeout=900, workdir=tmp_path
        )
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "composer-2.5"

    def test_copilot_gets_no_ask_user_and_takes_a_model(self, tmp_path):
        argv = engine_run.build_argv(
            "copilot", "go", model=None, timeout=900, workdir=tmp_path
        )
        assert "--no-ask-user" in argv
        assert "--model" not in argv
        assert argv[-1] == "go"
        argv = engine_run.build_argv(
            "copilot", "go", model="claude-sonnet-5", timeout=900, workdir=tmp_path
        )
        assert argv[argv.index("--model") + 1] == "claude-sonnet-5"

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

    def test_copilot_refusal_says_why_and_names_the_alternative(self):
        # A user who sees `--continue` in `copilot --help` needs to know r4t
        # refuses it on purpose, not that r4t is broken — and where to go
        # instead, since copilot does continue, by session id.
        with pytest.raises(engine_run.RunError) as exc:
            argv_for("copilot", continue_conversation=True)
        assert "machine's most recent session" in str(exc.value)
        assert "--session <uuid>" in str(exc.value)

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

    def test_no_agent_omits_the_convo_and_tell_steps_and_renumbers(self, tmp_path):
        text = engine_run.scaffold_prompt(tmp_path, "hi", agent=None)
        assert "a8s convo" not in text
        assert "Reply only by message" not in text
        assert "1. Read" in text
        assert "2. Stay idle" in text
        assert "3. Before exit" in text
        assert "4." not in text.split("Routed input:")[0]

    def test_agent_adds_the_convo_and_tell_steps(self, tmp_path):
        text = engine_run.scaffold_prompt(tmp_path, "hi", agent="my-node")
        assert "2. You are 'my-node'. Run `a8s convo my-node`" in text
        assert f"3. {engine_run.CLI_TELL_STEP}" in text
        assert "4. Stay idle" in text
        assert "5. Before exit" in text

    @pytest.mark.parametrize(
        "step", [engine_run.MCP_TELL_STEP, engine_run.CLI_TELL_STEP]
    )
    def test_the_callers_tell_step_follows_the_convo_step(self, tmp_path, step):
        text = engine_run.scaffold_prompt(tmp_path, "hi", agent="bob", tell_step=step)
        assert f"3. {step}" in text
        assert "Reply only by message" in step
        assert "no one reads this session's output" in step

    def test_a_tell_step_without_an_agent_is_not_sent(self, tmp_path):
        text = engine_run.scaffold_prompt(
            tmp_path, "hi", agent=None, tell_step=engine_run.MCP_TELL_STEP
        )
        assert "a8s_tell" not in text

    def test_the_mcp_step_names_the_tool_and_the_cli_step_names_the_command(self):
        assert "`a8s_tell`" in engine_run.MCP_TELL_STEP
        assert "`a8s_tell`" not in engine_run.CLI_TELL_STEP
        assert "tell <recipient> - <<'EOF'" in engine_run.CLI_TELL_STEP
        assert "tell <recipient> - <<'EOF'" in engine_run.MCP_TELL_STEP

    def test_resident_minutes_swaps_the_title_and_the_closing_steps(self, tmp_path):
        one_shot = engine_run.scaffold_prompt(tmp_path, "hi", agent="bob")
        resident = engine_run.scaffold_prompt(
            tmp_path, "hi", agent="bob", resident_minutes=45
        )
        assert one_shot.startswith("Smart cold boot:\n")
        assert resident.startswith("Smart cold boot (resident session):\n")
        assert "4. Resident session: it stays open while a turn is running" in resident
        assert "5. When the session window expires" in resident
        assert engine_run.LESSON_LINE_RULE in resident
        assert "Stay idle" not in resident and "Before exit" not in resident
        assert "Resident session" not in one_shot.split("\n", 1)[1]
        steps = lambda t: t.split("\n")[1:4]
        assert steps(one_shot) == steps(resident)

    def test_paths_are_absolute_and_under_dir(self, tmp_path):
        text = engine_run.scaffold_prompt(tmp_path, "hi", agent=None)
        assert str(tmp_path / "STATUS.md") in text
        assert str(tmp_path / "AGENTS.md") in text
        assert str(tmp_path / "LESSONS.md") in text


LESSON_DAY = "2026-10-17"


def write_lessons(dir_path: Path, lines: list[str]) -> Path:
    path = dir_path / "LESSONS.md"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def write_spillover(dir_path: Path, lines: list[str]) -> Path:
    path = dir_path / engine_run.LESSONS_ARCHIVE_NAME
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def scaffold_parts(prompt: str) -> tuple[str, str, str]:
    """(prelude, this turn's lessons note, routed input) of a scaffold."""
    head, _, routed = prompt.partition("\n\nRouted input:\n")
    prelude, _, note = head.partition("\n\n")
    return prelude, note, routed


def sha8(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()[:8]


def copies_of(seat: Path) -> list[Path]:
    folder = seat / "archive"
    return sorted(folder.glob("*.md")) if folder.exists() else []


def drive_engine(monkeypatch, tmp_path: Path, name: str) -> Path:
    """Point one preset's binary at a recorder that dumps its argv, and keep
    every other token the real preset carries. The recorder is the preset's
    argv[0], because the a8s MCP server is spliced in right after the binary.
    Returns the directory of recorded calls."""
    import rig as rig_module
    from conftest import write_path_executable

    calls = tmp_path / f"{name}-calls"
    calls.mkdir(exist_ok=True)
    script = write_path_executable(
        tmp_path / "bin",
        f"{name}-recorder",
        textwrap.dedent(
            f"""\
            import json, os, sys
            calls_dir = {str(calls)!r}
            n = len(os.listdir(calls_dir))
            with open(os.path.join(calls_dir, f"call-{{n:03d}}.json"), "w", encoding="utf-8", newline="") as f:
                json.dump(sys.argv[1:], f)
            """
        ),
    )
    real = rig_module.HARNESS_PRESETS[name]
    monkeypatch.setitem(
        rig_module.HARNESS_PRESETS, name, {**real, "invoke": [str(script), *real["invoke"][1:]]}
    )
    return calls


def recorded_argv(calls: Path) -> list[list[str]]:
    import json as jsonlib

    return [jsonlib.loads(p.read_text()) for p in sorted(calls.iterdir())]


@pytest.fixture
def turn(tmp_path, monkeypatch):
    """A recording claude preset and a fixed copy date. Returns a function that
    runs one `engine run` turn over `turn.seat` and hands back the prompt the
    engine received."""
    calls = drive_engine(monkeypatch, tmp_path, "claude")
    monkeypatch.setattr(engine_run, "_copy_day", lambda: LESSON_DAY)
    seat = tmp_path / "seat"
    seat.mkdir()

    def run(*args, message="do work"):
        before = set(calls.iterdir())
        assert engine_cli("claude", "run", "--dir", str(seat), *args, message) == 0
        [call] = set(calls.iterdir()) - before
        import json as jsonlib

        return jsonlib.loads(call.read_text())[-1]

    run.seat = seat
    return run


class TestLessonsConsolidationTrigger:
    def test_a_file_at_ninety_percent_of_the_line_cap_is_silent(self, turn):
        write_lessons(turn.seat, [f"- lesson {i}" for i in range(180)])
        prompt = turn()
        assert scaffold_parts(prompt)[1] == ""
        assert not (turn.seat / "archive").exists()

    def test_one_line_past_ninety_percent_asks_for_consolidation_and_saves_a_copy(
        self, turn
    ):
        lessons = write_lessons(turn.seat, [f"- lesson {i}" for i in range(181)])
        _, note, routed = scaffold_parts(turn())
        assert routed == "do work"
        assert note.startswith(f"Consolidate {lessons} before you answer any message. ")
        assert f"{lessons} is 181 lines / " in note
        assert "the cap is 200 lines / 35 KB" in note
        assert "Rewrite it to at most 150 lines and " in note
        [copy] = copies_of(turn.seat)
        assert copy.name == f"LESSONS-{LESSON_DAY}-{sha8(lessons.read_bytes())}.md"
        assert str(copy) in note

    def test_bytes_alone_past_ninety_percent_trigger(self, turn):
        # 1,001 bytes a line against 35 * 1024: 32 lines are 89% of the cap,
        # 33 are 92%, and both are far under 180 lines.
        write_lessons(turn.seat, [f"- {i:03d} " + "x" * 994 for i in range(32)])
        assert scaffold_parts(turn())[1] == ""
        assert copies_of(turn.seat) == []
        lessons = write_lessons(turn.seat, [f"- {i:03d} " + "x" * 994 for i in range(33)])
        _, note, _ = scaffold_parts(turn())
        assert f"{lessons} is 33 lines / " in note
        assert note.startswith("Consolidate ")
        assert len(copies_of(turn.seat)) == 1

    @pytest.mark.parametrize("size, due", [(900, False), (901, True)])
    def test_the_byte_threshold_is_strictly_past_ninety_percent(self, turn, size, due):
        body = ("x" * 99 + "\n") * 8 + "y" * (size - 800 - 1) + "\n"
        (turn.seat / "LESSONS.md").write_text(body, encoding="utf-8")
        assert len((turn.seat / "LESSONS.md").read_bytes()) == size
        note = scaffold_parts(turn("--lessons-cap-bytes", "1000"))[1]
        assert note.startswith("Consolidate ") is due
        assert len(copies_of(turn.seat)) == int(due)

    def test_custom_caps_set_the_trigger_and_the_target(self, turn):
        write_lessons(turn.seat, [f"- lesson {i}" for i in range(18)])
        caps = ("--lessons-cap", "20", "--lessons-cap-bytes", "4096")
        assert scaffold_parts(turn(*caps))[1] == ""
        lessons = write_lessons(turn.seat, [f"- lesson {i}" for i in range(19)])
        _, note, _ = scaffold_parts(turn(*caps))
        assert f"{lessons} is 19 lines / " in note
        assert "the cap is 20 lines / 4 KB" in note
        assert "Rewrite it to at most 15 lines and 3 KB." in note

    def test_a_small_byte_cap_triggers_a_short_file(self, turn):
        write_lessons(turn.seat, [f"- lesson {i}" for i in range(10)])
        _, note, _ = scaffold_parts(turn("--lessons-cap-bytes", "100"))
        assert note.startswith("Consolidate ")
        assert "the cap is 200 lines / 0.1 KB" in note

    def test_a_missing_lessons_file_is_silent(self, turn):
        assert scaffold_parts(turn())[1] == ""
        assert not (turn.seat / "archive").exists()

    def test_crlf_endings_count_as_one_line_break_each(self, turn):
        # 9 lines of 101 bytes with CRLF are 909 bytes on disk and 900 once
        # each CRLF counts as LF: not past 90% of a 1,000-byte cap.
        seat = turn.seat
        (seat / "LESSONS.md").write_bytes(("x" * 99 + "\r\n").encode() * 9)
        assert scaffold_parts(turn("--lessons-cap-bytes", "1000"))[1] == ""
        assert copies_of(seat) == []

    def test_a_crlf_file_over_the_line_threshold_counts_lines_not_carriage_returns(
        self, turn
    ):
        seat = turn.seat
        original = "".join(f"- lesson {i}\r\n" for i in range(181)).encode("utf-8")
        (seat / "LESSONS.md").write_bytes(original)
        _, note, _ = scaffold_parts(turn())
        assert f"{seat / 'LESSONS.md'} is 181 lines / " in note
        [copy] = copies_of(seat)
        assert copy.read_bytes() == original

    def test_a_file_that_is_not_utf8_neither_crashes_nor_triggers(self, turn):
        raw = "- café\n".encode("latin-1") * 250
        lessons = turn.seat / "LESSONS.md"
        lessons.write_bytes(raw)
        assert scaffold_parts(turn())[1] == ""
        assert lessons.read_bytes() == raw
        assert not (turn.seat / "archive").exists()

    def test_a_file_that_is_not_utf8_is_still_copied_when_a_spillover_is_due(self, turn):
        raw = "- café\n".encode("latin-1") * 250
        lessons = turn.seat / "LESSONS.md"
        lessons.write_bytes(raw)
        write_spillover(turn.seat, ["- old lesson"])
        _, note, _ = scaffold_parts(turn())
        assert note.startswith("Consolidate ")
        assert {p.read_bytes() for p in copies_of(turn.seat)} == {raw, b"- old lesson\n"}
        assert lessons.read_bytes() == raw


class TestLessonsSpillover:
    def test_a_spillover_alone_triggers_with_its_count_and_the_delete_instruction(
        self, turn
    ):
        seat = turn.seat
        lessons = write_lessons(seat, ["- one", "- two", "- three"])
        archive = write_spillover(seat, ["- old a", "", "- old b", "  ", "- old c", "- old d"])
        _, note, _ = scaffold_parts(turn())
        assert note.startswith(f"Consolidate {lessons} before you answer any message. ")
        assert (
            f"- {archive} holds 4 lines that no turn reads. Fold the lessons "
            f"that still hold into {lessons}, then delete {archive}." in note
        )
        names = sorted(p.name for p in copies_of(seat))
        assert names[0].startswith(f"LESSONS-{LESSON_DAY}-")
        assert names[1].startswith(f"LESSONS-ARCHIVE-{LESSON_DAY}-")

    def test_a_spillover_of_blank_lines_does_not_trigger(self, turn):
        seat = turn.seat
        write_lessons(seat, ["- one", "- two"])
        write_spillover(seat, ["", "   ", "\t"])
        assert scaffold_parts(turn())[1] == ""
        assert not (seat / "archive").exists()

    def test_an_empty_spillover_does_not_trigger(self, turn):
        write_lessons(turn.seat, ["- one"])
        (turn.seat / engine_run.LESSONS_ARCHIVE_NAME).write_bytes(b"")
        assert scaffold_parts(turn())[1] == ""

    def test_one_real_line_in_the_spillover_does_trigger(self, turn):
        write_lessons(turn.seat, ["- one"])
        write_spillover(turn.seat, ["", "- kept", ""])
        note = scaffold_parts(turn())[1]
        assert "holds 1 lines that no turn reads" in note

    def test_no_spillover_line_without_a_spillover(self, turn):
        write_lessons(turn.seat, [f"- lesson {i}" for i in range(181)])
        note = scaffold_parts(turn())[1]
        assert note.startswith("Consolidate ")
        assert "no turn reads" not in note
        assert "then delete" not in note
        [copy] = copies_of(turn.seat)
        assert copy.name.startswith("LESSONS-2")

    def test_a_spillover_with_no_lessons_file_copies_just_the_spillover(self, turn):
        archive = write_spillover(turn.seat, ["- old a"])
        note = scaffold_parts(turn())[1]
        assert "holds 1 lines that no turn reads" in note
        [copy] = copies_of(turn.seat)
        assert copy.name == (
            f"LESSONS-ARCHIVE-{LESSON_DAY}-{sha8(archive.read_bytes())}.md"
        )

    def test_a_crlf_spillover_counts_its_lines(self, turn):
        write_lessons(turn.seat, ["- one"])
        (turn.seat / engine_run.LESSONS_ARCHIVE_NAME).write_bytes(b"- a\r\n\r\n- b\r\n")
        assert "holds 2 lines that no turn reads" in scaffold_parts(turn())[1]


class TestLessonsCopies:
    def test_copies_are_byte_for_byte_and_named_with_date_and_hash(self, turn):
        seat = turn.seat
        live = "".join(f"- lesson {i}\r\n" for i in range(181)).encode("utf-8")
        spill = b"- old \xc3\xa9\r\n- older\n"
        (seat / "LESSONS.md").write_bytes(live)
        (seat / engine_run.LESSONS_ARCHIVE_NAME).write_bytes(spill)
        _, note, _ = scaffold_parts(turn())
        folder = seat / "archive"
        live_copy = folder / f"LESSONS-{LESSON_DAY}-{sha8(live)}.md"
        spill_copy = folder / f"LESSONS-ARCHIVE-{LESSON_DAY}-{sha8(spill)}.md"
        assert live_copy.read_bytes() == live
        assert spill_copy.read_bytes() == spill
        assert sorted(copies_of(seat)) == sorted([live_copy, spill_copy])
        assert f"Copies from before this turn: {live_copy}, {spill_copy}." in note

    def test_the_copy_date_is_the_utc_day_by_default(self, tmp_path, monkeypatch):
        from datetime import datetime, timezone

        calls = drive_engine(monkeypatch, tmp_path, "claude")
        seat = tmp_path / "seat"
        seat.mkdir()
        write_lessons(seat, [f"- lesson {i}" for i in range(181)])
        assert engine_cli("claude", "run", "--dir", str(seat), "go") == 0
        [copy] = copies_of(seat)
        assert copy.name.startswith(
            f"LESSONS-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-"
        )
        assert len(list(calls.iterdir())) == 1

    def test_identical_bytes_reuse_the_copy_across_turns(self, turn):
        seat = turn.seat
        write_lessons(seat, [f"- lesson {i}" for i in range(181)])
        first = scaffold_parts(turn())[1]
        [copy] = copies_of(seat)
        os.utime(copy, ns=(1, 1))
        second = scaffold_parts(turn(message="again"))[1]
        assert copies_of(seat) == [copy]
        assert copy.stat().st_mtime_ns == 1
        assert second == first

    def test_changed_bytes_add_a_second_copy_and_leave_the_first(self, turn):
        seat = turn.seat
        lessons = write_lessons(seat, [f"- lesson {i}" for i in range(181)])
        first_bytes = lessons.read_bytes()
        turn()
        write_lessons(seat, [f"- lesson {i}" for i in range(182)])
        turn()
        copies = copies_of(seat)
        assert len(copies) == 2
        assert first_bytes in {p.read_bytes() for p in copies}
        assert lessons.read_bytes() in {p.read_bytes() for p in copies}

    def test_a_copy_that_cannot_be_written_gives_the_warning_only_note(
        self, turn, capsys
    ):
        seat = turn.seat
        lessons = write_lessons(seat, [f"- lesson {i}" for i in range(181)])
        (seat / "archive").write_text("a file where the copy directory goes\n")
        _, note, _ = scaffold_parts(turn())
        assert "Consolidate" not in note
        assert note.startswith(f"{lessons} is 181 lines / ")
        assert "r4t could not save a copy of it, so do not edit existing lines" in note
        assert "Copies from before this turn" not in note
        err = capsys.readouterr().err
        assert any(
            line.startswith("r4t engine: no consolidation this turn:")
            for line in err.splitlines()
        )

    def test_a_copy_that_does_not_read_back_equal_gives_the_warning_only_note(
        self, turn, monkeypatch, capsys
    ):
        write_lessons(turn.seat, [f"- lesson {i}" for i in range(181)])

        def short_copy(path, data):
            path.write_bytes(data[:-10])

        monkeypatch.setattr(engine_run, "_atomic_write_bytes", short_copy)
        _, note, _ = scaffold_parts(turn())
        assert "Consolidate" not in note
        assert "r4t could not save a copy of it" in note
        err = capsys.readouterr().err
        assert "r4t engine: no consolidation this turn:" in err
        assert "does not match" in err

    def test_the_warning_only_note_also_covers_a_spillover(self, turn):
        seat = turn.seat
        write_lessons(seat, ["- one"])
        write_spillover(seat, ["- old"])
        (seat / "archive").write_text("not a directory\n")
        note = scaffold_parts(turn())[1]
        assert "r4t could not save a copy of it" in note
        assert "no turn reads" not in note

    def test_a_working_copy_directory_is_the_positive_control(self, turn, capsys):
        write_lessons(turn.seat, [f"- lesson {i}" for i in range(181)])
        _, note, _ = scaffold_parts(turn())
        assert note.startswith("Consolidate ")
        assert "no consolidation this turn" not in capsys.readouterr().err

    def test_r4t_leaves_lessons_and_the_spillover_byte_identical(self, turn):
        seat = turn.seat
        live = "".join(f"- lesson {i}\r\n" for i in range(150)).encode("utf-8")
        live += "".join(f"- lesson {i}\n" for i in range(150, 190)).encode("utf-8")
        spill = b"## old section\n- a\n\n- b"
        (seat / "LESSONS.md").write_bytes(live)
        (seat / engine_run.LESSONS_ARCHIVE_NAME).write_bytes(spill)
        assert scaffold_parts(turn())[1].startswith("Consolidate ")
        assert (seat / "LESSONS.md").read_bytes() == live
        assert (seat / engine_run.LESSONS_ARCHIVE_NAME).read_bytes() == spill
        assert sorted(p.name for p in seat.iterdir()) == [
            "LESSONS-ARCHIVE.md", "LESSONS.md", "archive"
        ]

    def test_an_idle_turn_gets_the_same_note(self, turn):
        write_lessons(turn.seat, [f"- lesson {i}" for i in range(181)])
        prompt = turn("--idle", message=engine_run.DEFAULT_IDLE_PROMPT)
        assert scaffold_parts(prompt)[1].startswith("Consolidate ")
        assert len(copies_of(turn.seat)) == 1

    def test_no_scaffold_makes_no_copy_and_sends_the_message_untouched(self, turn):
        seat = turn.seat
        write_lessons(seat, [f"- lesson {i}" for i in range(181)])
        write_spillover(seat, ["- old"])
        assert turn("--no-scaffold", message="raw text") == "raw text"
        assert not (seat / "archive").exists()
        assert scaffold_parts(turn(message="raw text"))[1].startswith("Consolidate ")
        assert len(copies_of(seat)) == 2


class TestPrepareLessonsCopies:
    def test_nothing_due_returns_none_without_a_word(self, tmp_path, capsys):
        write_lessons(tmp_path, [f"- lesson {i}" for i in range(180)])
        assert engine_run.prepare_lessons_copies(tmp_path) is None
        assert capsys.readouterr().err == ""
        assert not (tmp_path / "archive").exists()

    def test_due_returns_the_lessons_copy_then_the_spillover_copy(self, tmp_path):
        write_lessons(tmp_path, [f"- lesson {i}" for i in range(181)])
        write_spillover(tmp_path, ["- old"])
        lessons_copy, spill_copy = engine_run.prepare_lessons_copies(tmp_path)
        assert lessons_copy.parent == spill_copy.parent == tmp_path / "archive"
        assert lessons_copy.name.startswith("LESSONS-2")
        assert spill_copy.name.startswith("LESSONS-ARCHIVE-2")

    def test_caps_from_the_caller_move_the_threshold(self, tmp_path):
        write_lessons(tmp_path, [f"- lesson {i}" for i in range(10)])
        assert engine_run.prepare_lessons_copies(tmp_path, lessons_cap=12) is None
        assert engine_run.prepare_lessons_copies(tmp_path, lessons_cap=11) is not None

    @pytest.mark.skipif(
        os.name != "posix" or os.geteuid() == 0, reason="needs a mode bit root ignores"
    )
    @pytest.mark.parametrize("unreadable", ["LESSONS.md", "LESSONS-ARCHIVE.md"])
    def test_an_existing_file_that_cannot_be_read_refuses_the_rewrite(
        self, tmp_path, capsys, unreadable
    ):
        write_lessons(tmp_path, [f"- lesson {i}" for i in range(181)])
        write_spillover(tmp_path, ["- old"])
        target = tmp_path / unreadable
        target.chmod(0)
        try:
            assert engine_run.prepare_lessons_copies(tmp_path) is None
        finally:
            target.chmod(0o644)
        assert "no consolidation this turn" in capsys.readouterr().err
        note = engine_run.scaffold_prompt(tmp_path, "hi", agent="bob", copies=None)
        assert "could not save a copy" in note
        assert "Consolidate " not in note

    @pytest.mark.parametrize("failing", ["LESSONS.md", "LESSONS-ARCHIVE.md"])
    def test_a_one_time_read_error_refuses_the_rewrite(
        self, tmp_path, monkeypatch, capsys, failing
    ):
        import errno

        write_lessons(tmp_path, [f"- lesson {i}" for i in range(181)])
        write_spillover(tmp_path, ["- old"])
        real = Path.read_bytes
        failed = []

        # The source read inside the copy step, after the threshold check
        # has already read the file once and found it due.
        def flaky(self):
            caller = sys._getframe(1).f_code.co_name
            if self.name == failing and caller == "prepare_lessons_copies" and not failed:
                failed.append(self)
                raise OSError(errno.EIO, "Input/output error", str(self))
            return real(self)

        monkeypatch.setattr(Path, "read_bytes", flaky)
        assert engine_run.prepare_lessons_copies(tmp_path) is None
        assert failed
        assert "Input/output error" in capsys.readouterr().err

    def test_only_a_file_that_does_not_exist_is_skipped(self, tmp_path):
        write_spillover(tmp_path, ["- old"])
        assert not (tmp_path / "LESSONS.md").exists()
        (spill_copy,) = engine_run.prepare_lessons_copies(tmp_path)
        assert spill_copy.name.startswith("LESSONS-ARCHIVE-2")


class TestLessonsPreludeStability:
    def test_the_prelude_is_byte_identical_whether_or_not_the_note_appears(self, turn):
        seat = turn.seat
        quiet = turn()
        write_lessons(seat, [f"- lesson {i}" for i in range(181)])
        loud = turn()
        write_spillover(seat, ["- old"])
        write_lessons(seat, ["- one"])
        spill = turn()
        assert scaffold_parts(quiet)[1] == ""
        assert scaffold_parts(loud)[1].startswith("Consolidate ")
        assert scaffold_parts(spill)[1].startswith("Consolidate ")
        assert scaffold_parts(quiet)[0] == scaffold_parts(loud)[0] == scaffold_parts(spill)[0]

    def test_the_prelude_ignores_the_size_of_lessons_and_the_presence_of_a_spillover(
        self, tmp_path
    ):
        bare = engine_run.scaffold_prompt(tmp_path, "hi", agent="bob")
        write_lessons(tmp_path, [f"- lesson {i}" for i in range(5000)])
        write_spillover(tmp_path, ["- old"] * 500)
        copies = engine_run.prepare_lessons_copies(tmp_path)
        loud = engine_run.scaffold_prompt(tmp_path, "hi", agent="bob", copies=copies)
        warn = engine_run.scaffold_prompt(tmp_path, "hi", agent="bob", copies=None)
        assert bare == scaffold_parts(bare)[0] + "\n\nRouted input:\nhi"
        for prompt in (loud, warn):
            assert prompt != bare
            assert scaffold_parts(prompt)[0] == scaffold_parts(bare)[0]
            assert prompt.startswith(scaffold_parts(bare)[0] + "\n\n")

    def test_the_note_sits_between_the_prelude_and_the_routed_input(self, tmp_path):
        write_lessons(tmp_path, [f"- lesson {i}" for i in range(181)])
        copies = engine_run.prepare_lessons_copies(tmp_path)
        prompt = engine_run.scaffold_prompt(tmp_path, "hi", agent=None, copies=copies)
        prelude, note, routed = scaffold_parts(prompt)
        assert prelude.startswith("Smart cold boot:")
        assert note.startswith("Consolidate ")
        assert routed == "hi"
        assert prompt == f"{prelude}\n\n{note}\n\nRouted input:\nhi"

    def test_the_note_names_every_rule_of_the_rewrite(self, tmp_path):
        write_lessons(tmp_path, [f"- lesson {i}" for i in range(181)])
        copies = engine_run.prepare_lessons_copies(tmp_path)
        note = scaffold_parts(
            engine_run.scaffold_prompt(tmp_path, "hi", agent=None, copies=copies)
        )[1]
        assert "Every line is one lesson. Remove sections, headings and tiers." in note
        assert "Merge each duplicate into the lesson it repeats." in note
        assert "Drop a lesson only when a newer lesson supersedes it." in note
        assert "Never drop a live rule." in note
        assert "the one rewrite of LESSONS.md that the append-only rule allows" in note

    def test_the_line_rule_is_in_the_exit_step(self, tmp_path):
        prelude = scaffold_parts(engine_run.scaffold_prompt(tmp_path, "hi", agent=None))[0]
        assert engine_run.LESSON_LINE_RULE in prelude
        assert "one lesson, one short line each" in prelude
        assert "no sections, headings or tiers" in prelude


class TestRunTellStepAndMcp:
    MCP_FLAGS = {
        "claude": "--mcp-config",
        "codex": "-c",
        "copilot": "--additional-mcp-config",
        "ollama-claude": "--mcp-config",
        "ollama-codex": "-c",
    }
    NO_MCP = ["cursor", "agy", "opencode", "devin", "muse", "ollama-opencode"]

    def run_engine(self, monkeypatch, tmp_path, engine, *, agent="bob", scaffold=True):
        calls = drive_engine(monkeypatch, tmp_path, engine)
        seat = tmp_path / "seat"
        seat.mkdir(exist_ok=True)
        code = engine_run.execute(
            engine, "hello there",
            dir_path=seat, model="test-model" if engine.startswith("ollama") else None,
            agent=agent, timeout=30, scaffold=scaffold,
        )
        assert code == 0
        [argv] = recorded_argv(calls)
        return argv, seat

    def spliced(self, engine: str, argv: list[str]) -> str:
        flag = self.MCP_FLAGS[engine]
        at = argv.index("--") + 1 if "--" in argv else 0
        assert argv[at] == flag, argv
        return argv[at + 1]

    @pytest.mark.parametrize("engine", sorted(MCP_FLAGS))
    def test_a_named_agent_gets_the_a8s_server_and_the_mcp_tell_step(
        self, monkeypatch, tmp_path, engine
    ):
        argv, _ = self.run_engine(monkeypatch, tmp_path, engine)
        self.spliced(engine, argv)
        prompt = argv[-1]
        assert engine_run.MCP_TELL_STEP in prompt
        assert engine_run.CLI_TELL_STEP not in prompt
        assert "`a8s_tell`" in prompt

    def test_claude_gets_the_server_config_and_the_tool_in_its_allowlist(
        self, monkeypatch, tmp_path
    ):
        import json as jsonlib
        import rig as rig_module

        argv, _ = self.run_engine(monkeypatch, tmp_path, "claude")
        config = jsonlib.loads(self.spliced("claude", argv))
        assert list(config["mcpServers"]) == [rig_module.MCP_SERVER_NAME]
        assert config["mcpServers"][rig_module.MCP_SERVER_NAME]["command"]
        tools = argv[argv.index("--allowedTools") + 1].split()
        assert tools[-1] == rig_module.MCP_CLAUDE_TOOL == "mcp__a8s__tell"
        assert "Read" in tools

    def test_codex_gets_the_server_as_a_config_override(self, monkeypatch, tmp_path):
        argv, _ = self.run_engine(monkeypatch, tmp_path, "codex")
        assert self.spliced("codex", argv).startswith("mcp_servers.a8s=")

    @pytest.mark.parametrize("engine", NO_MCP)
    def test_an_engine_without_a_flag_idiom_keeps_the_shell_tell(
        self, monkeypatch, tmp_path, engine
    ):
        argv, seat = self.run_engine(monkeypatch, tmp_path, engine)
        prompt = argv[-1]
        assert engine_run.CLI_TELL_STEP in prompt
        assert engine_run.MCP_TELL_STEP not in prompt
        joined = " ".join(argv[:-1])
        for flag in ("--mcp-config", "--additional-mcp-config", "mcp_servers", "mcp__a8s__tell"):
            assert flag not in joined
        assert list(seat.iterdir()) == []

    @pytest.mark.parametrize("engine", ["claude", "codex"])
    def test_no_scaffold_leaves_argv_and_prompt_untouched(
        self, monkeypatch, tmp_path, engine
    ):
        argv, _ = self.run_engine(monkeypatch, tmp_path, engine, scaffold=False)
        joined = " ".join(argv[:-1])
        for flag in ("--mcp-config", "mcp_servers", "mcp__a8s__tell"):
            assert flag not in joined
        assert argv[-1] == "hello there"

    @pytest.mark.parametrize("engine", ["claude", "codex", "cursor"])
    def test_a_turn_with_no_agent_has_no_tell_step_and_no_server(
        self, monkeypatch, tmp_path, engine
    ):
        argv, _ = self.run_engine(monkeypatch, tmp_path, engine, agent=None)
        prompt = argv[-1]
        assert "Reply only by message" not in prompt
        assert "a8s convo" not in prompt
        assert prompt.endswith("hello there")
        for flag in ("--mcp-config", "--additional-mcp-config", "mcp_servers", "mcp__a8s__tell"):
            assert flag not in " ".join(argv[:-1])

    def test_the_cli_turn_with_an_agent_carries_the_server(self, tmp_path, monkeypatch):
        calls = drive_engine(monkeypatch, tmp_path, "claude")
        seat = tmp_path / "seat"
        seat.mkdir()
        assert engine_cli(
            "claude", "run", "--dir", str(seat), "--agent", "bob", "go"
        ) == 0
        [argv] = recorded_argv(calls)
        assert argv[0] == "--mcp-config"
        assert engine_run.MCP_TELL_STEP in argv[-1]

    def test_the_cli_turn_without_an_agent_does_not(self, tmp_path, monkeypatch):
        calls = drive_engine(monkeypatch, tmp_path, "claude")
        seat = tmp_path / "seat"
        seat.mkdir()
        assert engine_cli("claude", "run", "--dir", str(seat), "go") == 0
        [argv] = recorded_argv(calls)
        assert "--mcp-config" not in argv
        assert "Reply only by message" not in argv[-1]

    def test_the_cli_no_scaffold_turn_with_an_agent_does_not(self, tmp_path, monkeypatch):
        calls = drive_engine(monkeypatch, tmp_path, "claude")
        seat = tmp_path / "seat"
        seat.mkdir()
        assert engine_cli(
            "claude", "run", "--dir", str(seat), "--agent", "bob", "--no-scaffold", "go"
        ) == 0
        [argv] = recorded_argv(calls)
        assert "--mcp-config" not in argv
        assert argv[-1] == "go"


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

    @pytest.mark.skipif(os.name != "posix", reason="`#!` lines are POSIX")
    def test_exit_127_names_the_interpreter_the_engine_lacks(
        self, tmp_path, monkeypatch, capfd
    ):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        exe = bin_dir / "fake-node-cli"
        exe.write_text("#!/usr/bin/env r4t-no-such-node\n", encoding="utf-8")
        exe.chmod(0o755)
        monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]))
        import rig as rig_module

        original = dict(rig_module.HARNESS_PRESETS.get("claude", {}))
        rig_module.HARNESS_PRESETS["claude"] = {
            **original, "invoke": ["fake-node-cli", "{prompt}"],
        }
        try:
            code = engine_run.execute(
                "claude", "raw prompt text",
                dir_path=tmp_path, model=None, agent=None, timeout=30,
                scaffold=False,
            )
        finally:
            rig_module.HARNESS_PRESETS["claude"] = original
        assert code == 127
        err = capfd.readouterr().err
        assert f"fake-node-cli is {exe}" in err
        assert "r4t-no-such-node is not on PATH" in err

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

    def test_spawn_failure_names_the_path_searched(self, tmp_path):
        # #243: a bare, unresolvable name goes through PATH, and the operator
        # needs to see exactly what was searched — not a bare Errno 2 that
        # matches the shell's own PATH, not the wake's.
        env = {"PATH": str(tmp_path / "empty-bin")}
        with pytest.raises(engine_run.RunError) as exc:
            engine_run._spawn(["no-such-engine-binary-r4t"], tmp_path, 5, env=env)
        assert "no-such-engine-binary-r4t" in str(exc.value)
        assert str(tmp_path / "empty-bin") in str(exc.value)

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
        original = "\n".join(f"- lesson {i}" for i in range(6)) + "\n"
        lessons = tmp_path / "LESSONS.md"
        lessons.write_text(original, encoding="utf-8")
        code = engine_cli(
            "claude", "run", "--dir", str(tmp_path), "--lessons-cap", "5", "do work",
        )
        assert code == 0
        [call] = sorted(calls.iterdir())
        import json as jsonlib
        _, note, _ = scaffold_parts(jsonlib.loads(call.read_text())[0])
        assert note.startswith(f"Consolidate {lessons} before you answer any message. ")
        assert "the cap is 5 lines / " in note
        assert "at most 3 lines" in note
        assert lessons.read_text(encoding="utf-8") == original
        assert len(list((tmp_path / "archive").glob("LESSONS-*.md"))) == 1
        assert "rotated" not in capsys.readouterr().err

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

    def test_lessons_cap_bytes_flag_reaches_execute(self, tmp_path, monkeypatch, capsys):
        script, calls = fake_cli(tmp_path)
        import rig as rig_module

        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "claude",
            {**rig_module.HARNESS_PRESETS["claude"],
             "invoke": [sys.executable, str(script), "{prompt}"]},
        )
        body = [f"- {i} " + "x" * 96 for i in range(10)]  # 10 lines, ~1 KB
        lessons = write_lessons(tmp_path, body)
        original = lessons.read_bytes()
        code = engine_cli(
            "claude", "run", "--dir", str(tmp_path),
            "--lessons-cap-bytes", "1000", "do work",
        )
        assert code == 0
        [call] = sorted(calls.iterdir())
        import json as jsonlib
        _, note, _ = scaffold_parts(jsonlib.loads(call.read_text())[0])
        assert "the cap is 200 lines / 1 KB" in note
        assert "and 0.7 KB." in note
        assert lessons.read_bytes() == original
        [copy] = (tmp_path / "archive").glob("LESSONS-*.md")
        assert copy.read_bytes() == original

    @pytest.mark.parametrize("bad_value", ["-1", "0"])
    def test_lessons_cap_bytes_rejects_non_positive_values(
        self, tmp_path, capsys, bad_value
    ):
        with pytest.raises(SystemExit) as exc_info:
            engine_cli(
                "claude", "run", "--dir", str(tmp_path),
                "--lessons-cap-bytes", bad_value, "do work",
            )
        assert exc_info.value.code == 2
        assert "must be a positive integer" in capsys.readouterr().err


class TestEngineRunFlagsCli:
    def test_idle_and_continue_contradict(self, tmp_path, capsys):
        # #155 rule 4, enforced mechanically: an idle wake is a cold start.
        assert engine_cli("claude", "run", "--dir", str(tmp_path), "--idle", "--continue") == 2
        assert "idle" in capsys.readouterr().err
        assert not (tmp_path / ".engine-idle").exists()  # the latch never armed

    def test_continue_on_an_engine_that_cannot_exits_one(self, tmp_path, capsys):
        assert engine_cli("muse", "run", "--dir", str(tmp_path), "--continue", "go") == 1
        assert "session picker" in capsys.readouterr().err

    def test_continue_on_copilot_points_at_the_session_pin(self, tmp_path, capsys):
        assert engine_cli(
            "copilot", "run", "--dir", str(tmp_path), "--continue", "go"
        ) == 1
        assert "--session <uuid>" in capsys.readouterr().err

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
    """The relocated-copy fallbacks (ar3 unimportable) must carry the same
    capture-pgid-before-SIGTERM behavior as ar3.proc: a leader that has
    already been reaped when SIGKILL fires must not strand a SIGTERM-ignoring
    grandchild in the still-live process group."""

    SCENARIO = """
import importlib.abc, os, pathlib, signal, sys, time


class BlockAr3(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "ar3" or fullname.startswith("ar3."):
            raise ImportError("ar3 blocked (relocated-copy simulation)")
        return None


sys.meta_path.insert(0, BlockAr3())
sys.path.insert(0, {r4t_dir!r})

{import_and_kill}

pidfile = pathlib.Path({pidfile!r})
child_script = pathlib.Path({child_script!r})
import os
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


    def test_the_wake_routing_env_stops_at_the_turn(self, tmp_path, monkeypatch):
        """A nested `r4t engine run` inside the engine must key memory on its
        own --agent, so the child never inherits the wake's A8S_TURN_* names."""
        seen: list[dict] = []
        monkeypatch.setattr(
            engine_run, "_spawn",
            lambda argv, cwd, timeout, env=None: (seen.append(dict(env or {})), 0)[1],
        )
        env = {"PATH": os.environ.get("PATH", ""), "A8S_TURN_RECIPIENT": "nodea",
               "A8S_TURN_ENVELOPES": "[]", "TELL_OUTBOX_DIR": str(tmp_path)}
        engine_run.execute("claude", "hi", dir_path=tmp_path, model=None, agent=None,
                           timeout=5, scaffold=False, env=env)
        assert seen and not any(k.startswith("A8S_TURN_") for k in seen[0])
        assert seen[0]["TELL_OUTBOX_DIR"] == str(tmp_path)


AGENT_NAME = "Ada (agent)"
AGENT_EMAIL = "ada@example.com"
AGENT_IDENTITY = {
    "GIT_AUTHOR_NAME": AGENT_NAME,
    "GIT_COMMITTER_NAME": AGENT_NAME,
    "GIT_AUTHOR_EMAIL": AGENT_EMAIL,
    "GIT_COMMITTER_EMAIL": AGENT_EMAIL,
}
GIT_IDENTITY_NAMES = tuple(AGENT_IDENTITY)


def env_recording_cli(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in CLI that records the four git identity names as its own
    environment holds them (absent names absent), then exits 0."""
    script = tmp_path / "env-engine.py"
    record = tmp_path / "env-engine.json"
    script.write_text(
        textwrap.dedent(
            f"""\
            import json, os
            names = {list(GIT_IDENTITY_NAMES)!r}
            with open({str(record)!r}, "w", encoding="utf-8") as f:
                json.dump({{k: os.environ[k] for k in names if k in os.environ}}, f)
            """
        ),
        encoding="utf-8",
    )
    return script, record


class TestGitIdentity:
    """`--git-name` / `--git-email`: several engine seats under one Unix user
    otherwise all commit as the one shared git config identity. git's
    environment beats every config file, so the four names on the turn's
    child environment name the author and committer of each new commit the
    turn makes, in any repo."""

    @pytest.fixture
    def child_envs(self, monkeypatch):
        seen: list[dict] = []
        monkeypatch.setattr(
            engine_run, "_spawn",
            lambda argv, cwd, timeout, env=None: (seen.append(dict(env or {})), 0)[1],
        )
        return seen

    @staticmethod
    def _run(tmp_path, env, **kwargs):
        return engine_run.execute(
            "claude", "hi", dir_path=tmp_path, model=None, agent=None,
            timeout=5, scaffold=False, env=env, **kwargs,
        )

    @staticmethod
    def _base(tmp_path) -> dict[str, str]:
        return {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}

    def test_both_flags_add_exactly_the_four_names(self, tmp_path, child_envs):
        base = self._base(tmp_path)
        self._run(tmp_path, dict(base), git_name=AGENT_NAME, git_email=AGENT_EMAIL)
        assert child_envs == [{**base, **AGENT_IDENTITY}]

    def test_name_alone_sets_only_the_name_pair(self, tmp_path, child_envs):
        base = self._base(tmp_path)
        self._run(tmp_path, dict(base), git_name=AGENT_NAME)
        assert child_envs == [
            {**base, "GIT_AUTHOR_NAME": AGENT_NAME, "GIT_COMMITTER_NAME": AGENT_NAME}
        ]

    def test_email_alone_sets_only_the_email_pair(self, tmp_path, child_envs):
        base = self._base(tmp_path)
        self._run(tmp_path, dict(base), git_email=AGENT_EMAIL)
        assert child_envs == [
            {**base, "GIT_AUTHOR_EMAIL": AGENT_EMAIL, "GIT_COMMITTER_EMAIL": AGENT_EMAIL}
        ]

    @pytest.mark.parametrize(
        "identity",
        [
            {},
            {"git_name": None, "git_email": None},
            # An a8s var set to "" expands to `--git-name=`: the same as unset.
            {"git_name": "", "git_email": ""},
            {"git_name": "  ", "git_email": "\t"},
        ],
        ids=["absent", "none", "empty", "blank"],
    )
    def test_unset_leaves_the_child_env_byte_identical(
        self, tmp_path, child_envs, identity
    ):
        base = {
            **self._base(tmp_path),
            "GIT_AUTHOR_NAME": "Inherited Name",
            "GIT_COMMITTER_EMAIL": "inherited@example.com",
        }
        self._run(tmp_path, dict(base), **identity)
        assert child_envs == [base]

    def test_the_flag_wins_over_an_inherited_identity(self, tmp_path, child_envs):
        # The operator's per-agent setting is explicit; the daemon's ambient
        # environment is not.
        base = {
            **self._base(tmp_path),
            "GIT_AUTHOR_NAME": "Shared Login",
            "GIT_AUTHOR_EMAIL": "shared@example.com",
            "GIT_COMMITTER_NAME": "Shared Login",
            "GIT_COMMITTER_EMAIL": "shared@example.com",
        }
        self._run(tmp_path, dict(base), git_name=AGENT_NAME, git_email=AGENT_EMAIL)
        assert child_envs == [{**base, **AGENT_IDENTITY}]

    def test_an_inherited_environment_keeps_everything_else(
        self, tmp_path, child_envs, monkeypatch
    ):
        monkeypatch.setenv("R4T_TEST_KEPT", "kept")
        monkeypatch.delenv("GIT_AUTHOR_EMAIL", raising=False)
        self._run(tmp_path, None, git_name=AGENT_NAME)
        [child] = child_envs
        assert child["R4T_TEST_KEPT"] == "kept"
        assert child["GIT_AUTHOR_NAME"] == AGENT_NAME
        assert "GIT_AUTHOR_EMAIL" not in child

    @pytest.mark.parametrize("flag", ["git_name", "git_email"])
    @pytest.mark.parametrize(
        "bad",
        ["Ada\nEvil", "Ada\rEvil", "Ada\0", "Ada <ada@example.com>", "a>b"],
        ids=["newline", "return", "nul", "angle-open", "angle-close"],
    )
    def test_a_value_git_would_rewrite_is_refused_before_spawn(
        self, tmp_path, child_envs, flag, bad
    ):
        # git silently drops `<`, `>` and newlines from an ident, so a value
        # carrying one would commit as something the operator never typed.
        charged: list[int] = []
        with pytest.raises(engine_run.RunError, match=f"--{flag.replace('_', '-')}"):
            self._run(
                tmp_path, self._base(tmp_path),
                charge_hook=lambda: charged.append(1), **{flag: bad},
            )
        assert child_envs == []
        assert charged == []

    def test_the_memory_spawn_path_carries_the_identity(self, tmp_path, monkeypatch):
        import engine_memory
        import rig as rig_module

        class FakeTurn:
            def __init__(self, **_kwargs):
                pass

            def inject(self, prompt):
                return prompt

            def finish(self, output, exit_code):
                pass

        script, record = env_recording_cli(tmp_path)
        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "claude",
            {**rig_module.HARNESS_PRESETS["claude"],
             "invoke": [sys.executable, str(script), "{prompt}"]},
        )
        monkeypatch.setattr(engine_memory, "Turn", FakeTurn)
        monkeypatch.setattr(
            engine_run, "_spawn", lambda *a, **k: pytest.fail("memory turn used the plain spawn")
        )
        code = self._run(
            tmp_path, self._base(tmp_path),
            memory="small", git_name=AGENT_NAME, git_email=AGENT_EMAIL,
        )
        assert code == 0
        import json as jsonlib
        assert jsonlib.loads(record.read_text(encoding="utf-8")) == AGENT_IDENTITY

    def test_echo_names_the_identity_it_applies(self, tmp_path, child_envs, capsys):
        self._run(
            tmp_path, self._base(tmp_path), echo=True,
            git_name=AGENT_NAME, git_email=AGENT_EMAIL,
        )
        lines = [
            line for line in capsys.readouterr().err.splitlines()
            if line.startswith("r4t engine echo: env:")
        ]
        assert lines == [
            "r4t engine echo: env: GIT_AUTHOR_NAME='Ada (agent)' "
            "GIT_COMMITTER_NAME='Ada (agent)' GIT_AUTHOR_EMAIL=ada@example.com "
            "GIT_COMMITTER_EMAIL=ada@example.com"
        ]

    def test_echo_without_an_identity_prints_no_env_line(
        self, tmp_path, child_envs, capsys
    ):
        self._run(tmp_path, self._base(tmp_path), echo=True)
        assert "r4t engine echo: env:" not in capsys.readouterr().err

    def test_the_cli_flags_reach_the_child(self, tmp_path, monkeypatch):
        # The `--flag=value` spelling is what an a8s definition's
        # `--git-name=$GIT_NAME?` element expands to.
        import rig as rig_module

        script, record = env_recording_cli(tmp_path)
        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "claude",
            {**rig_module.HARNESS_PRESETS["claude"],
             "invoke": [sys.executable, str(script), "{prompt}"]},
        )
        for name in GIT_IDENTITY_NAMES:
            monkeypatch.delenv(name, raising=False)
        code = engine_cli(
            "claude", "run", "--dir", str(tmp_path), "--no-scaffold",
            f"--git-name={AGENT_NAME}", f"--git-email={AGENT_EMAIL}", "go",
        )
        assert code == 0
        import json as jsonlib
        assert jsonlib.loads(record.read_text(encoding="utf-8")) == AGENT_IDENTITY

    def test_the_cli_without_the_flags_passes_nothing(self, tmp_path, monkeypatch):
        import rig as rig_module

        script, record = env_recording_cli(tmp_path)
        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "claude",
            {**rig_module.HARNESS_PRESETS["claude"],
             "invoke": [sys.executable, str(script), "{prompt}"]},
        )
        for name in GIT_IDENTITY_NAMES:
            monkeypatch.delenv(name, raising=False)
        assert engine_cli("claude", "run", "--dir", str(tmp_path), "--no-scaffold", "go") == 0
        import json as jsonlib
        assert jsonlib.loads(record.read_text(encoding="utf-8")) == {}

    def test_the_cli_refuses_a_bad_value_and_spawns_nothing(
        self, tmp_path, monkeypatch, capsys
    ):
        script, calls = fake_cli(tmp_path)
        import rig as rig_module

        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "claude",
            {**rig_module.HARNESS_PRESETS["claude"],
             "invoke": [sys.executable, str(script), "{prompt}"]},
        )
        code = engine_cli(
            "claude", "run", "--dir", str(tmp_path), "--no-scaffold",
            "--git-email", "<ada@example.com>", "go",
        )
        assert code == 1
        assert "r4t engine: --git-email" in capsys.readouterr().err
        assert list(calls.iterdir()) == []


@pytest.fixture
def isolated_git(tmp_path, monkeypatch):
    """HOME and GIT_CONFIG_GLOBAL point into tmp_path, so the configured
    identity is the fixture's and the user's own git config is never read."""
    home = tmp_path / "home"
    home.mkdir()
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = Configured Owner\n\temail = owner@example.com\n"
        "[init]\n\tdefaultBranch = main\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in (*GIT_IDENTITY_NAMES, "EMAIL", "GIT_DIR", "GIT_WORK_TREE"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs a real git")
class TestGitIdentityRealCommit:
    """The positive control: a turn's child runs a real `git commit`, and the
    commit's author and committer are whatever the turn's environment says."""

    CONFIGURED = ["Configured Owner", "owner@example.com"] * 2

    @pytest.fixture
    def committing_engine(self, tmp_path, monkeypatch, isolated_git):
        import rig as rig_module

        script = tmp_path / "committing-engine.py"
        script.write_text(
            textwrap.dedent(
                """\
                import subprocess, sys
                repo = sys.argv[1]
                subprocess.run(["git", "init", "-q", repo], check=True)
                subprocess.run(
                    ["git", "-C", repo, "commit", "-q", "--allow-empty", "-m", "turn"],
                    check=True,
                )
                ident = subprocess.run(
                    ["git", "-C", repo, "log", "-1", "--format=%an%n%ae%n%cn%n%ce"],
                    capture_output=True, text=True, check=True,
                ).stdout
                with open(repo + ".ident", "w", encoding="utf-8") as f:
                    f.write(ident)
                """
            ),
            encoding="utf-8",
        )
        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "claude",
            {**rig_module.HARNESS_PRESETS["claude"],
             "invoke": [sys.executable, str(script), "{prompt}"]},
        )

        def commit(repo: Path, *flags: str) -> list[str]:
            code = engine_cli(
                "claude", "run", "--dir", str(tmp_path), "--no-scaffold", *flags,
                str(repo),
            )
            assert code == 0
            return Path(f"{repo}.ident").read_text(encoding="utf-8").splitlines()

        return commit

    def test_the_commit_is_authored_and_committed_as_the_agent(
        self, tmp_path, committing_engine
    ):
        assert committing_engine(
            tmp_path / "with-flags",
            f"--git-name={AGENT_NAME}", f"--git-email={AGENT_EMAIL}",
        ) == [AGENT_NAME, AGENT_EMAIL, AGENT_NAME, AGENT_EMAIL]

    def test_without_the_flags_git_keeps_its_configured_identity(
        self, tmp_path, committing_engine
    ):
        assert committing_engine(tmp_path / "without-flags") == self.CONFIGURED

    def test_one_flag_leaves_the_other_half_to_git_config(
        self, tmp_path, committing_engine
    ):
        assert committing_engine(
            tmp_path / "name-only", f"--git-name={AGENT_NAME}",
        ) == [AGENT_NAME, "owner@example.com", AGENT_NAME, "owner@example.com"]

    def test_the_flag_beats_an_inherited_git_environment(
        self, tmp_path, committing_engine, monkeypatch
    ):
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Shared Login")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "Shared Login")
        assert committing_engine(
            tmp_path / "over-inherited", f"--git-name={AGENT_NAME}",
        ) == [AGENT_NAME, "owner@example.com", AGENT_NAME, "owner@example.com"]


ORIGINAL_AUTHOR = ["Original Author", "author@example.com"]


@pytest.mark.skipif(shutil.which("git") is None, reason="needs a real git")
class TestGitIdentityRewrittenCommit:
    """A rewrite keeps its author. `git commit --amend` and `git rebase` carry
    the commit's author over and record whoever rewrote it as the committer,
    whatever `GIT_AUTHOR_*` says. So a turn's identity names the committer of
    a commit it rewrites, and the original author stays."""

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        name, email = ORIGINAL_AUTHOR
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
        }
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            env=env, capture_output=True, text=True, check=True,
        ).stdout

    @classmethod
    def _commit_file(cls, repo: Path, name: str) -> None:
        (repo / name).write_text(name, encoding="utf-8")
        cls._git(repo, "add", name)
        cls._git(repo, "commit", "-q", "-m", name)

    @classmethod
    def _repo(cls, path: Path) -> Path:
        path.mkdir()
        cls._git(path, "init", "-q")
        cls._commit_file(path, "base")
        return path

    @pytest.fixture
    def rewriting_engine(self, tmp_path, monkeypatch, isolated_git):
        """Runs one turn that does `git -C <repo> <git_args>`, and returns
        the head commit's subject, author and committer."""
        import rig as rig_module

        script = tmp_path / "rewriting-engine.py"

        def rewrite(repo: Path, *git_args: str) -> list[str]:
            script.write_text(
                textwrap.dedent(
                    f"""\
                    import subprocess, sys
                    subprocess.run(
                        ["git", "-C", sys.argv[1], *{list(git_args)!r}], check=True
                    )
                    """
                ),
                encoding="utf-8",
            )
            monkeypatch.setitem(
                rig_module.HARNESS_PRESETS, "claude",
                {**rig_module.HARNESS_PRESETS["claude"],
                 "invoke": [sys.executable, str(script), "{prompt}"]},
            )
            code = engine_cli(
                "claude", "run", "--dir", str(tmp_path), "--no-scaffold",
                f"--git-name={AGENT_NAME}", f"--git-email={AGENT_EMAIL}",
                str(repo),
            )
            assert code == 0
            return self._git(
                repo, "log", "-1", "--format=%s%n%an%n%ae%n%cn%n%ce"
            ).splitlines()

        return rewrite

    def test_an_amend_keeps_the_author_and_names_the_agent_committer(
        self, tmp_path, rewriting_engine
    ):
        repo = self._repo(tmp_path / "amend")
        assert rewriting_engine(repo, "commit", "-q", "--amend", "--no-edit") == [
            "base", *ORIGINAL_AUTHOR, AGENT_NAME, AGENT_EMAIL,
        ]

    def test_a_rebase_keeps_the_author_and_names_the_agent_committer(
        self, tmp_path, rewriting_engine
    ):
        repo = self._repo(tmp_path / "rebase")
        trunk = self._git(repo, "symbolic-ref", "--short", "HEAD").strip()
        self._git(repo, "checkout", "-q", "-b", "topic")
        self._commit_file(repo, "topic")
        self._git(repo, "checkout", "-q", trunk)
        self._commit_file(repo, "trunk")
        self._git(repo, "checkout", "-q", "topic")
        assert rewriting_engine(repo, "rebase", "-q", trunk) == [
            "topic", *ORIGINAL_AUTHOR, AGENT_NAME, AGENT_EMAIL,
        ]


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


class TestEffortUnsupportedEngines:
    """An engine with no effort flag runs anyway: the value is dropped with
    one warning (owner ruling 2026-10-20)."""

    @pytest.mark.parametrize("engine", ["cursor", "devin"])
    def test_effort_is_dropped_and_the_engine_still_runs(
        self, engine, tmp_path, monkeypatch, capsys
    ):
        import rig as rig_module

        rig_module._EFFORT_DROPPED.clear()
        calls = drive_engine(monkeypatch, tmp_path, engine)
        code = engine_cli(
            engine, "run", "--dir", str(tmp_path), "--no-scaffold",
            "--effort", "high", "hello",
        )
        assert code == 0
        [argv] = recorded_argv(calls)
        assert "high" not in argv and "--effort" not in argv
        err = capsys.readouterr().err
        assert f"engine '{engine}' does not support --effort; 'high' ignored" in err
        assert err.count("does not support --effort") == 1
