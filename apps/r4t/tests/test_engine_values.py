"""`r4t engine <id> models|efforts` — what --model and --effort accept."""
from __future__ import annotations

import json
import subprocess

import pytest

from engines import values
from rig import HARNESS_PRESETS
from r4t import main as r4t_main

CLAUDE_HELP = """\
  --effort <level>                      Effort level
  --model <model>                       Model for the current session. Provide
                                        an alias for the latest model (e.g.
                                        'fable', 'opus', or 'sonnet') or a
                                        model's full name.
  -n, --name <name>                     Set a display name
"""


def fake_cli(monkeypatch, outputs, calls=None):
    """Answer `subprocess.run` from {argv-tuple: stdout or CompletedProcess}."""

    def run(argv, **kwargs):
        if calls is not None:
            calls.append((tuple(argv), kwargs))
        out = outputs[tuple(argv)]
        if isinstance(out, BaseException):
            raise out
        if isinstance(out, subprocess.CompletedProcess):
            return out
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(values.subprocess, "run", run)


class TestEfforts:
    @pytest.mark.parametrize(
        "preset",
        sorted(p for p, e in HARNESS_PRESETS.items() if e.get("effort_values")),
    )
    def test_a_preset_with_a_vocabulary_answers_exactly_it(self, preset):
        found, note = values.efforts(preset)
        assert found == HARNESS_PRESETS[preset]["effort_values"]
        assert note is None

    @pytest.mark.parametrize("preset", ["codex", "opencode", "ollama-codex", "ollama-opencode"])
    def test_a_free_value_preset_answers_empty_with_a_note(self, preset):
        found, note = values.efforts(preset)
        assert found == []
        assert "any non-empty effort value" in note

    @pytest.mark.parametrize("preset", ["cursor", "devin", "ollama"])
    def test_a_preset_without_an_effort_flag_refuses_with_apply_efforts_wording(self, preset):
        with pytest.raises(values.ValuesError, match=f"engine '{preset}' does not support --effort"):
            values.efforts(preset)

    def test_an_unknown_id_is_refused(self):
        with pytest.raises(values.ValuesError, match="not an engine or preset id"):
            values.efforts("emacs")


class TestModels:
    def test_claude_parses_the_aliases_from_help(self, monkeypatch):
        fake_cli(monkeypatch, {("claude", "--help"): CLAUDE_HELP.replace("'sonnet'", "'sonnet' or 'haiku'")})
        found, note = values.models("claude")
        assert found == ["fable", "opus", "sonnet", "haiku"]
        assert note == "claude also accepts a full model name"

    def test_claude_falls_back_when_help_has_no_list(self, monkeypatch):
        fake_cli(monkeypatch, {("claude", "--help"): "  --model <model>  A model\n  -n x\n"})
        assert values.models("claude")[0] == ["fable", "opus", "sonnet"]

    def test_claude_falls_back_when_the_cli_is_missing(self, monkeypatch):
        fake_cli(monkeypatch, {("claude", "--help"): FileNotFoundError("claude")})
        assert values.models("claude")[0] == ["fable", "opus", "sonnet"]

    @pytest.mark.parametrize("preset", ["ollama", "ollama-claude", "ollama-codex"])
    def test_ollama_presets_read_the_ollama_table(self, monkeypatch, preset):
        table = (
            "NAME                       ID              SIZE      MODIFIED\n"
            "qwen3.6:latest             07d35212591f    23 GB     2 months ago\n"
            "qwen3:4b                   abc             2 GB      1 day ago\n"
        )
        fake_cli(monkeypatch, {("ollama", "list"): table})
        assert values.models(preset) == (["qwen3.6:latest", "qwen3:4b"], None)

    def test_agy_ids_come_from_the_live_list(self, monkeypatch):
        calls = []
        fake_cli(
            monkeypatch,
            {("agy", "models"): "Fetching available models...\nflash-high\tFlash (High)\nflash-low\tFlash (Low)\n"},
            calls,
        )
        assert values.models("agy") == (["flash-high", "flash-low"], None)
        kwargs = calls[0][1]
        assert kwargs["timeout"] == 30 and kwargs["capture_output"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL

    def test_cursor_reads_ids_from_the_binary_the_preset_names(self, monkeypatch):
        binary = HARNESS_PRESETS["cursor"]["invoke"][0]
        fake_cli(monkeypatch, {(binary, "models"): "Available models\n\nauto - Auto (default)\ncomposer-2.5 - Composer\n"})
        assert values.models("cursor") == (["auto", "composer-2.5"], None)

    def test_devin_ids_skip_headers_and_aliases(self, monkeypatch):
        text = (
            "Available models (2 families)\n\n"
            "SWE-2 (swe-2)\n  aliases: swe\n"
            "  swe-2-high      SWE-2 High  [262K context, Free]\n"
            "  swe-2-max       SWE-2 Max  [262K context, Free]\n"
        )
        fake_cli(monkeypatch, {("devin", "models", "list"): text})
        assert values.models("devin") == (["swe-2-high", "swe-2-max"], None)

    @pytest.mark.parametrize("preset", ["codex", "copilot", "opencode", "muse"])
    def test_a_list_less_engine_refuses_and_says_where_the_string_goes(self, preset):
        with pytest.raises(values.ValuesError, match=f"{preset} has no list verb; .*(passed through|checked by)"):
            values.models(preset)

    def test_a_failing_cli_is_a_values_error_naming_it(self, monkeypatch):
        bad = subprocess.CompletedProcess(["agy", "models"], 3, stdout="", stderr="boom")
        fake_cli(monkeypatch, {("agy", "models"): bad})
        with pytest.raises(values.ValuesError, match=r"`agy models` failed \(exit 3\): boom"):
            values.models("agy")

    def test_a_timeout_is_a_values_error(self, monkeypatch):
        fake_cli(monkeypatch, {("ollama", "list"): subprocess.TimeoutExpired("ollama", 30)})
        with pytest.raises(values.ValuesError, match="timed out"):
            values.models("ollama")


class TestVerbs:
    def test_verbs_follow_the_effort_flag_and_the_list_source(self):
        assert values.verbs("claude") == ["efforts", "models"]
        assert values.verbs("codex") == ["efforts"]
        assert values.verbs("cursor") == ["models"]
        assert values.verbs("muse") == ["efforts"]

class TestCli:
    def test_efforts_print_one_per_line(self, capsys):
        assert r4t_main(["engine", "claude", "efforts"]) == 0
        assert capsys.readouterr().out.split() == ["low", "medium", "high", "xhigh", "max"]

    def test_a_trailing_word_is_refused_on_every_python(self, capsys):
        # argparse before 3.14 rejects the stray word itself and exits; 3.14
        # hands it to the command as the prompt, which refuses it. Both exit 2.
        try:
            code = r4t_main(["engine", "claude", "--json", "efforts", "unexpected"])
        except SystemExit as exc:
            code = exc.code
        assert code == 2
        err = capsys.readouterr().err
        assert "unexpected" in err
        assert "takes no further argument" in err or "unrecognized arguments" in err

    def test_json_shape(self, capsys):
        assert r4t_main(["engine", "claude", "efforts", "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == {
            "values": ["low", "medium", "high", "xhigh", "max"],
            "note": None,
        }

    def test_the_note_goes_to_stderr_with_exit_zero(self, capsys):
        assert r4t_main(["engine", "codex", "efforts"]) == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith("r4t engine: engine 'codex' takes any")

    def test_a_refusal_is_exit_one_on_stderr(self, capsys):
        assert r4t_main(["engine", "cursor", "efforts"]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "r4t engine: engine 'cursor' does not support --effort" in captured.err

    def test_models_through_the_cli(self, monkeypatch, capsys):
        fake_cli(monkeypatch, {("ollama", "list"): "NAME ID\nm1:latest x\n"})
        assert r4t_main(["engine", "ollama", "models"]) == 0
        assert capsys.readouterr().out == "m1:latest\n"

    def test_list_shows_the_verbs(self, capsys):
        assert r4t_main(["engine", "list"]) == 0
        out = capsys.readouterr().out
        assert "[quota, run, check, efforts, models]" in out
        assert "[quota, models]" in out
