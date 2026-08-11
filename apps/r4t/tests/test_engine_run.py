from __future__ import annotations

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
            with open(os.path.join(calls_dir, f"call-{{n:03d}}.json"), "w") as f:
                json.dump(sys.argv[1:], f)
            print("fake engine ran")
            """
        ),
        encoding="utf-8",
    )
    return script, calls


class TestBuildArgv:
    def test_unsupported_engine_names_the_supported_set(self):
        with pytest.raises(engine_run.RunError, match="claude"):
            engine_run.build_argv("opencode", "hi", model=None, timeout=900)

    def test_claude_gets_model_flag_and_no_extras(self):
        argv = engine_run.build_argv("claude", "do the thing", model="sonnet", timeout=900)
        assert argv[0] == "claude"
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "sonnet"
        assert argv[-1] == "do the thing"
        assert "--print-timeout" not in argv
        assert "--no-ask-user" not in argv

    def test_codex_model_flag_uses_dash_m(self):
        argv = engine_run.build_argv("codex", "fix it", model="o4", timeout=900)
        assert "-m" in argv
        assert argv[argv.index("-m") + 1] == "o4"
        assert argv[-1] == "fix it"

    def test_cursor_model_flag(self):
        argv = engine_run.build_argv("cursor", "go", model="opus", timeout=900)
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "opus"

    def test_copilot_gets_no_ask_user_and_rejects_model(self):
        argv = engine_run.build_argv("copilot", "go", model=None, timeout=900)
        assert "--no-ask-user" in argv
        assert argv[-1] == "go"
        with pytest.raises(engine_run.RunError, match="--model"):
            engine_run.build_argv("copilot", "go", model="anything", timeout=900)

    def test_copilot_does_not_double_no_ask_user_if_preset_already_carries_it(
        self, monkeypatch
    ):
        monkeypatch.setitem(
            engine_run.HARNESS_PRESETS,
            "copilot",
            {**engine_run.HARNESS_PRESETS["copilot"], "invoke": [
                "copilot", "--no-ask-user", "--allow-all-tools", "-p", "{prompt}",
            ]},
        )
        argv = engine_run.build_argv("copilot", "go", model=None, timeout=900)
        assert argv.count("--no-ask-user") == 1

    def test_agy_always_gets_print_timeout_matching_the_run_timeout(self):
        argv = engine_run.build_argv("agy", "go", model=None, timeout=45)
        assert "--print-timeout" in argv
        assert argv[argv.index("--print-timeout") + 1] == "45s"

    def test_agy_model_resolves_live_against_agy_models(self, monkeypatch):
        monkeypatch.setattr(
            engine_run, "resolve_agy_model", lambda query, **k: "Gemini 3.6 Flash Low"
        )
        argv = engine_run.build_argv("agy", "go", model="flash", timeout=900)
        assert "Gemini 3.6 Flash Low" in argv
        assert "{model}" not in argv

    def test_agy_model_resolution_failure_is_a_run_error(self, monkeypatch):
        def boom(query, **k):
            raise RigError("no such model")

        monkeypatch.setattr(engine_run, "resolve_agy_model", boom)
        with pytest.raises(engine_run.RunError, match="no such model"):
            engine_run.build_argv("agy", "go", model="nonsense", timeout=900)


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


class TestLessonsWarning:
    def test_short_lessons_is_silent(self, tmp_path, capsys):
        (tmp_path / "LESSONS.md").write_text("- one\n- two\n", encoding="utf-8")
        engine_run.warn_if_lessons_oversized(tmp_path)
        assert capsys.readouterr().err == ""

    def test_long_lessons_warns_once_on_stderr(self, tmp_path, capsys):
        (tmp_path / "LESSONS.md").write_text(
            "\n".join(f"- lesson {i}" for i in range(250)) + "\n", encoding="utf-8"
        )
        engine_run.warn_if_lessons_oversized(tmp_path)
        err = capsys.readouterr().err
        assert "LESSONS.md" in err
        assert err.count("\n") == 1

    def test_missing_lessons_is_silent(self, tmp_path, capsys):
        engine_run.warn_if_lessons_oversized(tmp_path)
        assert capsys.readouterr().err == ""


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


class TestCapabilities:
    def test_run_engines_report_both_verbs(self):
        for name in engine_run.RUN_ENGINES:
            assert engines.capabilities(name) == ["quota", "run"]

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
        assert engine_cli("opencode", "run", "hi") == 1
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
