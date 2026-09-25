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


# A seat's own header: its title, the rule it keeps about its size, and the
# traps it re-reads every turn. These are the lines FIFO used to drop first.
HEADER = [
    "# LESSONS",
    "Keep each lesson to one line; older lines rotate to LESSONS-ARCHIVE.md.",
    "Standing traps: read STATUS.md before acting.",
    "",
]


def write_lessons(dir_path: Path, lines: list[str]) -> Path:
    path = dir_path / "LESSONS.md"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def lines_of(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def archive_of(dir_path: Path) -> list[str]:
    return lines_of(dir_path / engine_run.LESSONS_ARCHIVE_NAME)


def assert_lossless(original: list[str], live: list[str], archive: list[str]) -> None:
    """Every original line is still on disk: the rotation may duplicate a line
    into the archive, never lose one."""
    from collections import Counter

    missing = Counter(original) - (Counter(live) + Counter(archive))
    assert not missing, f"lines lost: {sorted(missing)}"


def byte_size(lines: list[str]) -> int:
    return sum(len(line.encode("utf-8")) + 1 for line in lines)


class TestLessonsPinnedHeader:
    def test_lines_above_the_first_section_heading_never_rotate(self, tmp_path):
        body = ["## Lessons", *(f"- lesson {i}" for i in range(250))]
        lessons = write_lessons(tmp_path, HEADER + body)
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=200)
        # The cut lands inside "## Lessons", so the heading stays on top of
        # what is left of its section, and the archive keeps it in place.
        assert lines_of(lessons) == (
            HEADER + ["## Lessons"] + [f"- lesson {i}" for i in range(55, 250)]
        )
        assert archive_of(tmp_path) == (
            ["## Lessons"] + [f"- lesson {i}" for i in range(55)]
        )

    def test_the_marker_pins_everything_above_it(self, tmp_path):
        # The first `## ` heading is line 2, so without the marker only the
        # title would be pinned and the standing traps would rotate first.
        head = [
            "# LESSONS",
            "## Standing traps",
            "- never push to main",
            engine_run.LESSONS_ROTATE_MARKER,
        ]
        body = [f"- lesson {i}" for i in range(30)]
        lessons = write_lessons(tmp_path, head + body)
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=20)
        assert lines_of(lessons) == head + [f"- lesson {i}" for i in range(14, 30)]
        assert archive_of(tmp_path) == [f"- lesson {i}" for i in range(14)]

    def test_the_marker_overrides_the_heading_rule_when_it_sits_higher(self, tmp_path):
        marker = engine_run.LESSONS_ROTATE_MARKER
        body = ["- early note", "## Lessons", *(f"- lesson {i}" for i in range(20))]
        lessons = write_lessons(tmp_path, [marker] + body)
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=12)
        assert lines_of(lessons) == (
            [marker, "## Lessons"] + [f"- lesson {i}" for i in range(10, 20)]
        )
        assert archive_of(tmp_path)[0] == "- early note"

    def test_a_file_with_no_section_heading_and_no_marker_rotates_from_the_top(
        self, tmp_path
    ):
        lessons = write_lessons(tmp_path, [f"- lesson {i}" for i in range(12)])
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=10)
        assert lines_of(lessons) == [f"- lesson {i}" for i in range(2, 12)]
        assert archive_of(tmp_path) == ["- lesson 0", "- lesson 1"]

    def test_a_title_with_no_section_heading_stays_on_top_of_what_is_left(
        self, tmp_path
    ):
        body = ["# LESSONS", *(f"- lesson {i}" for i in range(12))]
        lessons = write_lessons(tmp_path, body)
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=10)
        assert lines_of(lessons) == ["# LESSONS"] + [f"- lesson {i}" for i in range(3, 12)]
        assert archive_of(tmp_path) == ["# LESSONS", "- lesson 0", "- lesson 1", "- lesson 2"]

    def test_a_cut_inside_a_subsection_keeps_every_enclosing_heading(self, tmp_path):
        body = ["## Tools", "### git", *(f"- git {i}" for i in range(10))]
        lessons = write_lessons(tmp_path, body)
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=8)
        assert lines_of(lessons) == (
            ["## Tools", "### git"] + [f"- git {i}" for i in range(4, 10)]
        )
        assert archive_of(tmp_path) == (
            ["## Tools", "### git"] + [f"- git {i}" for i in range(4)]
        )

    def test_a_cut_on_a_section_boundary_carries_no_heading(self, tmp_path):
        body = ["## Old", "- old 0", "- old 1", "## New", "- new 0", "- new 1"]
        lessons = write_lessons(tmp_path, body)
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=3)
        assert lines_of(lessons) == ["## New", "- new 0", "- new 1"]
        assert archive_of(tmp_path) == ["## Old", "- old 0", "- old 1"]

    def test_blank_lines_at_the_cut_move_with_it(self, tmp_path):
        lessons = write_lessons(tmp_path, ["- a", "- b", "", "", "- c", "- d"])
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=4)
        assert lines_of(lessons) == ["- c", "- d"]
        assert archive_of(tmp_path) == ["- a", "- b", "", ""]

    def test_a_header_over_the_cap_on_its_own_moves_everything_below_it_and_says_so(
        self, tmp_path, capsys
    ):
        head = [f"- rule {i}" for i in range(6)] + [engine_run.LESSONS_ROTATE_MARKER]
        lessons = write_lessons(tmp_path, head + ["- new 0", "- new 1"])
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=5)
        assert lines_of(lessons) == head
        assert archive_of(tmp_path) == ["- new 0", "- new 1"]
        err = capsys.readouterr().err.splitlines()
        assert err[0].startswith("r4t engine: rotated 2 lines from ")
        assert "header" in err[1] and "over" in err[1]

    def test_an_all_header_file_over_the_cap_writes_no_archive(self, tmp_path, capsys):
        head = [f"- rule {i}" for i in range(6)] + [engine_run.LESSONS_ROTATE_MARKER]
        lessons = write_lessons(tmp_path, head)
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=5)
        assert lines_of(lessons) == head
        assert not (tmp_path / engine_run.LESSONS_ARCHIVE_NAME).exists()
        assert "header" in capsys.readouterr().err


class TestLessonsByteCap:
    def test_a_byte_heavy_file_under_the_line_cap_rotates(self, tmp_path):
        # 60 lessons of 1,000 characters: far under 200 lines, about 59 KB.
        body = [f"- {i:03d} " + "x" * 994 for i in range(60)]
        lessons = write_lessons(tmp_path, body)
        engine_run.rotate_lessons_if_oversized(tmp_path)
        assert (tmp_path / engine_run.LESSONS_ARCHIVE_NAME).exists()
        # 1,001 bytes a line against 35 * 1024: 35 lines fit, 36 do not.
        assert lines_of(lessons) == body[25:]
        assert archive_of(tmp_path) == body[:25]
        assert len(lessons.read_bytes()) <= engine_run.LESSONS_CAP_BYTES

    def test_both_caps_hold_and_no_line_moves_that_did_not_have_to(self, tmp_path):
        body = [f"- short {i:02d}" for i in range(12)]
        body += ["- long " + "x" * 50 + f" {i:02d}" for i in range(8)]
        lessons = write_lessons(tmp_path, body)
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=10, cap_bytes=400)
        live, archive = lines_of(lessons), archive_of(tmp_path)
        assert len(live) <= 10 and len(lessons.read_bytes()) <= 400
        # The line cap alone would keep ten lines; the byte cap binds first.
        assert len(live) < 10
        # One line fewer moved would break a cap.
        back = [archive[-1]] + live
        assert len(back) > 10 or byte_size(back) > 400
        assert live == body[len(archive):]

    def test_a_single_line_over_the_byte_cap_empties_the_file_losslessly(self, tmp_path):
        lessons = write_lessons(tmp_path, ["- " + "y" * 600])
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=10, cap_bytes=500)
        assert lessons.read_text(encoding="utf-8") == ""
        assert archive_of(tmp_path) == ["- " + "y" * 600]


class Killed(BaseException):
    """A kill mid-rotation: nothing after the raising write runs."""


class TestLessonsRotationIsLossless:
    def test_a_kill_between_the_archive_write_and_the_live_rewrite_only_duplicates(
        self, tmp_path, monkeypatch
    ):
        original = HEADER + ["## Lessons"] + [f"- lesson {i} " + "z" * 300 for i in range(150)]
        lessons = write_lessons(tmp_path, original)
        real_write = engine_run._atomic_write_bytes
        order = []

        def dies_on_the_live_rewrite(path, data):
            order.append(path.name)
            if path.name == "LESSONS.md":
                raise Killed
            real_write(path, data)

        monkeypatch.setattr(engine_run, "_atomic_write_bytes", dies_on_the_live_rewrite)
        with pytest.raises(Killed):
            engine_run.rotate_lessons_if_oversized(tmp_path)
        assert order == [engine_run.LESSONS_ARCHIVE_NAME, "LESSONS.md"]
        assert lines_of(lessons) == original  # the live file is untouched
        assert archive_of(tmp_path)  # and its oldest lines are already archived
        assert_lossless(original, lines_of(lessons), archive_of(tmp_path))

        # The next turn's rotation completes. The first attempt's lines sit in
        # the archive twice; none is missing, and the header is still on top.
        monkeypatch.setattr(engine_run, "_atomic_write_bytes", real_write)
        engine_run.rotate_lessons_if_oversized(tmp_path)
        assert_lossless(original, lines_of(lessons), archive_of(tmp_path))
        assert lines_of(lessons)[:4] == HEADER

    def test_a_kill_on_the_archive_write_changes_nothing(self, tmp_path, monkeypatch):
        original = [f"- lesson {i}" for i in range(12)]
        lessons = write_lessons(tmp_path, original)

        def dies(path, data):
            raise Killed

        monkeypatch.setattr(engine_run, "_atomic_write_bytes", dies)
        with pytest.raises(Killed):
            engine_run.rotate_lessons_if_oversized(tmp_path, cap=10)
        assert lines_of(lessons) == original
        assert not (tmp_path / engine_run.LESSONS_ARCHIVE_NAME).exists()

    def test_random_files_rotate_losslessly_within_both_caps(self, tmp_path):
        import random

        rng = random.Random(300)
        marker = engine_run.LESSONS_ROTATE_MARKER
        for case in range(300):
            seat = tmp_path / f"seat-{case}"
            seat.mkdir()
            lines = []
            for _ in range(rng.randint(0, 80)):
                roll = rng.random()
                if roll < 0.08:
                    lines.append("#" * rng.randint(1, 4) + f" section {len(lines)}")
                elif roll < 0.15:
                    lines.append("")
                elif roll < 0.17:
                    lines.append(marker)
                else:
                    lines.append(f"- {len(lines)} " + "w" * rng.randint(0, 120))
            write_lessons(seat, lines)
            cap, cap_bytes = rng.randint(1, 60), rng.randint(40, 4000)
            engine_run.rotate_lessons_if_oversized(seat, cap=cap, cap_bytes=cap_bytes)
            live, archive = lines_of(seat / "LESSONS.md"), archive_of(seat)
            assert_lossless(lines, live, archive)
            pinned = engine_run._pinned_count(lines)
            assert live[:pinned] == lines[:pinned], f"case {case}: header moved"
            if archive:
                assert archive == lines[pinned:pinned + len(archive)]
            if byte_size(lines[:pinned]) <= cap_bytes and pinned <= cap:
                # A header that fits leaves room for the rest to fit.
                assert len(live) <= cap and byte_size(live) <= cap_bytes, f"case {case}"


class TestLessonsLineEndings:
    """Rotation rewrites both files, so it writes one line ending throughout:
    LF, on every platform, which also makes the byte cap exact on disk."""

    def crlf(self, lines: list[str]) -> bytes:
        return "".join(f"{line}\r\n" for line in lines).encode("utf-8")

    def test_a_crlf_file_rotates_to_lf_in_both_files(self, tmp_path):
        original = HEADER + ["## Lessons"] + [f"- lesson {i}" for i in range(20)]
        lessons = tmp_path / "LESSONS.md"
        lessons.write_bytes(self.crlf(original))
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=15)
        archive = tmp_path / engine_run.LESSONS_ARCHIVE_NAME
        assert b"\r" not in lessons.read_bytes()
        assert b"\r" not in archive.read_bytes()
        assert lines_of(lessons)[:4] == HEADER
        assert_lossless(original, lines_of(lessons), archive_of(tmp_path))

    @pytest.mark.parametrize("live_crlf", [True, False])
    def test_live_and_archive_that_disagree_end_up_with_one_ending(
        self, tmp_path, live_crlf
    ):
        live = [f"- new {i}" for i in range(12)]
        old = [f"- old {i}" for i in range(3)]
        lf = lambda lines: "".join(f"{line}\n" for line in lines).encode("utf-8")
        (tmp_path / "LESSONS.md").write_bytes(self.crlf(live) if live_crlf else lf(live))
        archive = tmp_path / engine_run.LESSONS_ARCHIVE_NAME
        archive.write_bytes(lf(old) if live_crlf else self.crlf(old))
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=10)
        for path in (tmp_path / "LESSONS.md", archive):
            assert b"\r" not in path.read_bytes(), path.name
        assert archive_of(tmp_path) == old + ["- new 0", "- new 1"]

    def test_the_byte_cap_holds_on_disk_for_a_crlf_file(self, tmp_path):
        lessons = tmp_path / "LESSONS.md"
        lessons.write_bytes(self.crlf([f"- {i} " + "x" * 60 for i in range(20)]))
        engine_run.rotate_lessons_if_oversized(tmp_path, cap_bytes=700)
        assert len(lessons.read_bytes()) <= 700

    def test_a_file_that_is_not_utf8_is_left_alone(self, tmp_path, capsys):
        raw = "- café\n".encode("latin-1") * 250
        lessons = tmp_path / "LESSONS.md"
        lessons.write_bytes(raw)
        engine_run.rotate_lessons_if_oversized(tmp_path)
        assert lessons.read_bytes() == raw
        assert not (tmp_path / engine_run.LESSONS_ARCHIVE_NAME).exists()
        assert "not UTF-8" in capsys.readouterr().err
        assert scaffold_parts(engine_run.scaffold_prompt(tmp_path, "hi", agent=None))[1] == ""


def scaffold_parts(prompt: str) -> tuple[str, str, str]:
    """(prelude, this turn's lessons note, routed input) of a scaffold."""
    head, _, routed = prompt.partition("\n\nRouted input:\n")
    prelude, _, note = head.partition("\n\n")
    return prelude, note, routed


APPEND_ONLY = (
    "Do not append a lesson that repeats one already here; the idle pass "
    "consolidates. Do not edit existing lines."
)


def assert_append_only(prompt: str) -> None:
    """The turn may append and nothing else: the nudge says so, and no line of
    the prompt grants an edit to an existing lesson."""
    _, note, _ = scaffold_parts(prompt)
    assert note.endswith(APPEND_ONLY), note
    assert "Idle fold" not in prompt
    assert "allows" not in prompt


class TestLessonsSoftCapNudge:
    def test_under_the_soft_cap_the_scaffold_is_byte_identical(self, tmp_path):
        bare = engine_run.scaffold_prompt(tmp_path, "hi", agent="bob")
        write_lessons(tmp_path, [f"- lesson {i}" for i in range(160)])
        assert engine_run.scaffold_prompt(tmp_path, "hi", agent="bob") == bare

    def test_over_the_soft_cap_by_lines_one_line_follows_the_prelude(self, tmp_path):
        bare_prelude, bare_note, _ = scaffold_parts(
            engine_run.scaffold_prompt(tmp_path, "hi", agent="bob")
        )
        assert bare_note == ""
        write_lessons(tmp_path, [f"- lesson {i}" for i in range(161)])
        prompt = engine_run.scaffold_prompt(tmp_path, "hi", agent="bob")
        prelude, note, routed = scaffold_parts(prompt)
        assert prelude == bare_prelude  # the cached prefix does not move
        assert prompt.startswith(bare_prelude + "\n\n" + note)
        assert "\n" not in note
        lessons = tmp_path / "LESSONS.md"
        assert note.startswith(f"{lessons} is 161 lines / ")
        assert "KB of 200 lines / 35 KB" in note
        assert note.endswith(APPEND_ONLY)
        assert_append_only(prompt)
        assert routed == "hi"

    def test_over_the_soft_cap_by_bytes_alone(self, tmp_path):
        write_lessons(tmp_path, [f"- {i:03d} " + "x" * 994 for i in range(30)])
        _, note, _ = scaffold_parts(engine_run.scaffold_prompt(tmp_path, "hi", agent=None))
        assert f"{tmp_path / 'LESSONS.md'} is 30 lines / 29.3 KB of 200 lines / 35 KB" in note

    def test_the_soft_cap_follows_the_caps_in_force(self, tmp_path):
        write_lessons(tmp_path, [f"- lesson {i}" for i in range(8)])
        quiet = engine_run.scaffold_prompt(tmp_path, "hi", agent=None, lessons_cap=10)
        assert scaffold_parts(quiet)[1] == ""
        write_lessons(tmp_path, [f"- lesson {i}" for i in range(9)])
        loud = engine_run.scaffold_prompt(tmp_path, "hi", agent=None, lessons_cap=10)
        assert "is 9 lines / " in scaffold_parts(loud)[1]
        assert "of 10 lines / 35 KB" in scaffold_parts(loud)[1]
        small = engine_run.scaffold_prompt(
            tmp_path, "hi", agent=None, lessons_cap_bytes=100
        )
        assert "of 200 lines / 0.1 KB" in scaffold_parts(small)[1]

    def test_the_cli_turn_carries_the_nudge(self, tmp_path, monkeypatch):
        script, calls = fake_cli(tmp_path)
        import rig as rig_module

        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "claude",
            {**rig_module.HARNESS_PRESETS["claude"],
             "invoke": [sys.executable, str(script), "{prompt}"]},
        )
        write_lessons(tmp_path, [f"- lesson {i}" for i in range(9)])
        code = engine_cli(
            "claude", "run", "--dir", str(tmp_path), "--lessons-cap", "10", "do work",
        )
        assert code == 0
        [call] = sorted(calls.iterdir())
        import json as jsonlib
        [prompt] = jsonlib.loads(call.read_text())
        assert "is 9 lines / " in scaffold_parts(prompt)[1]


class TestLessonsArchivePointer:
    POINTER = "Older lessons: {} (grep it; do not read it whole)."

    def test_no_archive_no_pointer(self, tmp_path):
        assert "Older lessons" not in engine_run.scaffold_prompt(tmp_path, "hi", agent=None)

    def test_an_empty_archive_earns_no_pointer(self, tmp_path):
        bare = engine_run.scaffold_prompt(tmp_path, "hi", agent=None)
        (tmp_path / engine_run.LESSONS_ARCHIVE_NAME).write_text("", encoding="utf-8")
        assert engine_run.scaffold_prompt(tmp_path, "hi", agent=None) == bare

    def test_a_non_empty_archive_is_named_in_the_read_step(self, tmp_path):
        archive = tmp_path / engine_run.LESSONS_ARCHIVE_NAME
        archive.write_text("- old\n", encoding="utf-8")
        prelude, note, _ = scaffold_parts(
            engine_run.scaffold_prompt(tmp_path, "hi", agent=None)
        )
        [read_step] = [line for line in prelude.splitlines() if line.startswith("1. ")]
        assert read_step.endswith(self.POINTER.format(archive))
        assert note == ""

    def test_the_prelude_holds_still_while_the_archive_grows(self, tmp_path):
        archive = tmp_path / engine_run.LESSONS_ARCHIVE_NAME
        archive.write_text("- old\n", encoding="utf-8")
        first = engine_run.scaffold_prompt(tmp_path, "message one", agent="bob")
        archive.write_text("- old\n" * 500, encoding="utf-8")
        second = engine_run.scaffold_prompt(tmp_path, "message two", agent="bob")
        assert scaffold_parts(first)[0] == scaffold_parts(second)[0]

    def test_a_rotation_turns_the_pointer_on_for_the_same_turn(self, tmp_path):
        write_lessons(tmp_path, [f"- lesson {i}" for i in range(12)])
        engine_run.rotate_lessons_if_oversized(tmp_path, cap=10)
        prompt = engine_run.scaffold_prompt(tmp_path, "hi", agent=None)
        archive = tmp_path / engine_run.LESSONS_ARCHIVE_NAME
        assert self.POINTER.format(archive) in scaffold_parts(prompt)[0]


FOLD_DAY = "2026-09-24"


@pytest.fixture
def fold_turn(tmp_path, monkeypatch):
    """A recording claude preset and a fixed fold date. Returns a function that
    runs one `engine run` turn and hands back the prompt it received."""
    script, calls = fake_cli(tmp_path)
    import json as jsonlib
    import rig as rig_module

    monkeypatch.setitem(
        rig_module.HARNESS_PRESETS, "claude",
        {**rig_module.HARNESS_PRESETS["claude"],
         "invoke": [sys.executable, str(script), "{prompt}"]},
    )
    monkeypatch.setattr(engine_run, "_fold_day", lambda: FOLD_DAY)
    seat = tmp_path / "seat"
    seat.mkdir()

    def turn(*args):
        before = set(calls.iterdir())
        assert engine_cli("claude", "run", "--dir", str(seat), *args) == 0
        [call] = set(calls.iterdir()) - before
        [prompt] = jsonlib.loads(call.read_text())
        return prompt

    turn.seat = seat
    return turn


class TestLessonsIdleFold:
    def fold_paths(self, seat: Path) -> tuple[Path, Path]:
        folds = seat / "archive"
        return folds / f"lessons-fold-{FOLD_DAY}-source.md", folds / f"lessons-fold-{FOLD_DAY}.md"

    def test_an_idle_turn_over_the_soft_cap_asks_for_a_fold(self, fold_turn):
        seat = fold_turn.seat
        original = HEADER + ["## Lessons"] + [f"- lesson {i}" for i in range(170)]
        write_lessons(seat, original)
        prompt = fold_turn("--idle")
        source, ledger = self.fold_paths(seat)
        assert lines_of(source) == original  # the fold's lossless copy
        prelude, note, routed = scaffold_parts(prompt)
        assert routed == engine_run.DEFAULT_IDLE_PROMPT
        assert note.startswith(f"Idle fold: {seat / 'LESSONS.md'} is 175 lines / ")
        assert str(source) in note and str(ledger) in note
        assert "Keep lines 1-4" in note  # the header, by number
        assert "Never drop a live rule." in note
        assert "supersedes" in note
        for outcome in ("kept", "merged-into", "dropped-superseded"):
            assert outcome in note
        # The fold replaces the nudge rather than joining it.
        assert APPEND_ONLY not in prompt

    def test_an_idle_turn_under_the_soft_cap_is_unchanged(self, fold_turn):
        seat = fold_turn.seat
        write_lessons(seat, [f"- lesson {i}" for i in range(150)])
        prompt = fold_turn("--idle")
        assert prompt == engine_run.scaffold_prompt(
            seat, engine_run.DEFAULT_IDLE_PROMPT, agent=None
        )
        assert not (seat / "archive").exists()

    def test_a_routed_turn_over_the_soft_cap_is_append_only(self, fold_turn):
        # No copy is made on a routed turn, so it may not edit a line.
        seat = fold_turn.seat
        write_lessons(seat, [f"- lesson {i}" for i in range(170)])
        assert_append_only(fold_turn("do work"))
        assert not (seat / "archive").exists()

    def test_an_idle_turn_whose_copy_cannot_be_written_is_append_only(
        self, fold_turn, capsys
    ):
        seat = fold_turn.seat
        write_lessons(seat, [f"- lesson {i}" for i in range(170)])
        (seat / "archive").write_text("a file where the fold directory goes\n")
        assert_append_only(fold_turn("--idle"))
        assert "no fold this turn" in capsys.readouterr().err

    def test_an_idle_turn_whose_copy_does_not_match_is_append_only(
        self, fold_turn, monkeypatch, capsys
    ):
        seat = fold_turn.seat
        write_lessons(seat, [f"- lesson {i}" for i in range(170)])

        def short_copy(path, data):
            path.write_bytes(data[:-10])

        monkeypatch.setattr(engine_run, "_atomic_write_bytes", short_copy, raising=False)
        assert_append_only(fold_turn("--idle"))
        assert "does not match" in capsys.readouterr().err

    def test_the_fold_copy_is_byte_for_byte(self, fold_turn):
        seat = fold_turn.seat
        original = "".join(f"- lesson {i}\r\n" for i in range(170)).encode("utf-8")
        (seat / "LESSONS.md").write_bytes(original)
        prompt = fold_turn("--idle")
        source, _ = self.fold_paths(seat)
        assert source.read_bytes() == original
        assert scaffold_parts(prompt)[1].startswith("Idle fold: ")

    def test_a_fold_killed_mid_rewrite_loses_nothing(self, tmp_path, monkeypatch):
        # The rewrite a fold permits dies 20 bytes in; its copy holds every line.
        script = tmp_path / "killed-rewrite.py"
        script.write_text(
            "import pathlib\n"
            "lessons = pathlib.Path('LESSONS.md')\n"
            "lessons.write_bytes(lessons.read_bytes()[:20])\n",
            encoding="utf-8",
        )
        import rig as rig_module

        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "claude",
            {**rig_module.HARNESS_PRESETS["claude"],
             "invoke": [sys.executable, str(script), "{prompt}"]},
        )
        monkeypatch.setattr(engine_run, "_fold_day", lambda: FOLD_DAY)
        original = HEADER + ["## Lessons"] + [f"- lesson {i}" for i in range(170)]
        lessons = write_lessons(tmp_path, original)
        assert engine_cli("claude", "run", "--dir", str(tmp_path), "--idle") == 0
        assert len(lessons.read_bytes()) == 20
        source, _ = self.fold_paths(tmp_path)
        assert_lossless(original, lines_of(lessons), lines_of(source))

    def test_a_custom_idle_prompt_keeps_the_fold(self, fold_turn):
        seat = fold_turn.seat
        write_lessons(seat, [f"- lesson {i}" for i in range(170)])
        prompt = fold_turn("--idle", "tidy the notes")
        _, note, routed = scaffold_parts(prompt)
        assert note.startswith("Idle fold: ")
        assert routed == "tidy the notes"
        # No header in this file, so no header clause to keep.
        assert "Keep line" not in note

    def test_no_scaffold_carries_no_fold(self, fold_turn):
        seat = fold_turn.seat
        write_lessons(seat, [f"- lesson {i}" for i in range(170)])
        assert fold_turn("--idle", "--no-scaffold") == engine_run.DEFAULT_IDLE_PROMPT
        assert not (seat / "archive").exists()

    def test_one_fold_a_day_and_its_copy_is_never_overwritten(self, fold_turn):
        seat = fold_turn.seat
        original = [f"- lesson {i}" for i in range(170)]
        lessons = write_lessons(seat, original)
        assert scaffold_parts(fold_turn("--idle"))[1].startswith("Idle fold: ")
        source, _ = self.fold_paths(seat)
        # The model folded, but not far enough; a routed turn re-arms the latch.
        write_lessons(seat, original[:165])
        fold_turn("real work")
        # Lines added since this morning's copy have no copy, so no edits.
        again = fold_turn("--idle")
        assert_append_only(again)
        assert lines_of(source) == original
        assert lines_of(lessons) == original[:165]

    def test_the_marker_names_the_header_span(self, fold_turn):
        seat = fold_turn.seat
        head = ["# LESSONS", engine_run.LESSONS_ROTATE_MARKER]
        write_lessons(seat, head + [f"- lesson {i}" for i in range(170)])
        assert "Keep lines 1-2" in scaffold_parts(fold_turn("--idle"))[1]



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


QUOTALESS = {"devin"}  # see engines/devin.py


class TestCapabilities:
    def test_run_engines_report_every_verb(self):
        for name in engine_run.RUN_ENGINES:
            expected = ["run", "check"] if name in QUOTALESS else [
                "quota", "run", "check"
            ]
            assert engines.capabilities(name) == expected

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
        code = engine_cli(
            "claude", "run", "--dir", str(tmp_path),
            "--lessons-cap-bytes", "500", "do work",
        )
        assert code == 0
        assert len(list(calls.iterdir())) == 1
        assert len(lessons.read_bytes()) <= 500
        assert_lossless(body, lines_of(lessons), archive_of(tmp_path))
        assert "rotated" in capsys.readouterr().err

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
