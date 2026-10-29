"""`r4t engine <id> models|efforts` — what --model and --effort accept."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time

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

    @pytest.mark.parametrize("preset", ["codex", "copilot", "opencode"])
    def test_a_list_less_engine_refuses_and_says_where_the_string_goes(self, preset):
        with pytest.raises(values.ValuesError, match=f"{preset} has no list verb; .*(passed through|checked by)"):
            values.models(preset)

    def _fake_muse(self, tmp_path, monkeypatch, body):
        script = tmp_path / "muse"
        handshake = (
            "import json, sys\n"
            "assert json.loads(sys.stdin.readline())['method'] == 'initialize'\n"
            "print(json.dumps({'id': 1, 'result': {}}), flush=True)\n"
        )
        script.write_text("#!/usr/bin/env python3\n" + handshake + body)
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
        monkeypatch.setenv("R4T_TEST_INHERIT", "kept")

    MUSE_OK = (
        "import json, os, sys\n"
        "assert sys.argv[1:] == ['serve', '--disable-request-user-input']\n"
        "for line in sys.stdin:\n"
        "    m = json.loads(line)\n"
        "    if m.get('method') == 'model/list':\n"
        "        rows = [{'modelId': 'muse-a'}, {'modelId': os.environ['R4T_TEST_INHERIT']}, {'x': 1}]\n"
        "        print('noise'); print(json.dumps({'id': 2, 'result': {'models': rows}}), flush=True)\n"
    )

    def test_muse_ids_come_from_msp_model_list_with_inherited_env(self, tmp_path, monkeypatch):
        self._fake_muse(tmp_path, monkeypatch, self.MUSE_OK)
        assert values.models("muse") == (["muse-a", "kept"], None)

    def test_muse_model_list_error_is_a_values_error(self, tmp_path, monkeypatch):
        self._fake_muse(tmp_path, monkeypatch, (
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    if 'model/list' in line:\n"
            "        print(json.dumps({'id': 2, 'error': {'message': 'nope'}}), flush=True)\n"))
        with pytest.raises(values.ValuesError, match="model/list failed"):
            values.models("muse")

    def test_muse_exit_before_answer_is_a_values_error(self, tmp_path, monkeypatch):
        self._fake_muse(tmp_path, monkeypatch, "import sys\nsys.exit(3)\n")
        with pytest.raises(values.ValuesError, match="exited before answering|closed its input"):
            values.models("muse")

    def test_muse_empty_catalog_is_refused(self, tmp_path, monkeypatch):
        self._fake_muse(tmp_path, monkeypatch, (
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    if 'model/list' in line:\n"
            "        print(json.dumps({'id': 2, 'result': {'models': []}}), flush=True)\n"))
        with pytest.raises(values.ValuesError, match="listed no models"):
            values.models("muse")

    def test_muse_timeout_is_a_values_error(self, tmp_path, monkeypatch):
        self._fake_muse(tmp_path, monkeypatch, "import sys, time\nsys.stdin.read()\ntime.sleep(30)\n")
        monkeypatch.setattr(values, "LIST_TIMEOUT_SECONDS", 1)
        with pytest.raises(values.ValuesError, match="timed out"):
            values.models("muse")

    @pytest.mark.parametrize("result", [True, "bad", [1]])
    def test_muse_malformed_catalog_is_a_values_error(self, tmp_path, monkeypatch, result):
        self._fake_muse(tmp_path, monkeypatch, (
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    if 'model/list' in line:\n"
            f"        print(json.dumps({{'id': 2, 'result': {result!r}}}), flush=True)\n"))
        with pytest.raises(values.ValuesError, match="no models list"):
            values.models("muse")

    def test_muse_server_request_cannot_impersonate_catalog(self, tmp_path, monkeypatch):
        collision = (
            "        print(json.dumps({'id': 2, 'method': 'approval', "
            "'result': {'models': [{'modelId': 'wrong'}]}}), flush=True)\n"
        )
        self._fake_muse(tmp_path, monkeypatch, self.MUSE_OK.replace("        rows =", collision + "        rows ="))
        assert values.models("muse")[0] == ["muse-a", "kept"]

    def test_muse_noisy_output_is_bounded(self, tmp_path, monkeypatch):
        self._fake_muse(tmp_path, monkeypatch, (
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    if 'model/list' in line:\n"
            "        while True: print('noise', flush=True)\n"))
        monkeypatch.setattr(values, "LIST_TIMEOUT_SECONDS", 0.1)
        with pytest.raises(values.ValuesError, match="timed out|response limit"):
            values.models("muse")

    MUSE_FORK = (
        "import os, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import signal, time\\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\ntime.sleep(60)'])\n"
        "open(os.environ['R4T_TEST_CHILD'], 'w', encoding='utf-8').write(str(child.pid))\n"
    )

    @staticmethod
    def _gone(pid, seconds=3):
        def state():
            try:
                with open(f"/proc/{pid}/stat") as f:
                    return f.read().rsplit(")", 1)[1].split()[0]
            except OSError:
                return None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if state() in (None, "Z"):
                return True
            time.sleep(0.05)
        return False

    @pytest.mark.skipif(not os.path.isdir("/proc"), reason="reads /proc")
    @pytest.mark.parametrize("ending", ["success", "early-exit", "timeout"])
    def test_muse_child_that_outlives_the_leader_is_terminated(self, tmp_path, monkeypatch, ending):
        pidfile = tmp_path / "child.pid"
        monkeypatch.setenv("R4T_TEST_CHILD", str(pidfile))
        tail = {
            "success": "",
            "early-exit": "sys.exit(3)\n",
            "timeout": "sys.stdin.read()\ntime.sleep(30)\n",
        }[ending]
        body = self.MUSE_FORK + (
            "import time\n" + self.MUSE_OK.split("\n", 1)[1] if ending == "success" else "import time\n" + tail
        )
        self._fake_muse(tmp_path, monkeypatch, body)
        monkeypatch.setattr(values, "LIST_TIMEOUT_SECONDS", 1)
        try:
            if ending == "success":
                assert values.models("muse") == (["muse-a", "kept"], None)
            else:
                with pytest.raises(values.ValuesError):
                    values.models("muse")
            child = int(pidfile.read_text())
            assert self._gone(child), "forked helper survived the catalog read"
        finally:
            if pidfile.exists():
                child = int(pidfile.read_text())
                if not self._gone(child, seconds=0.1):
                    try:
                        os.kill(child, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_muse_launch_owns_a_group_on_posix_and_windows(self, monkeypatch):
        seen = []

        def popen(argv, **kwargs):
            seen.append(kwargs)
            raise OSError("stop here")

        monkeypatch.setattr(values.subprocess, "Popen", popen)
        real = values.launch_options
        for windows in (False, True):
            monkeypatch.setattr(values, "launch_options", lambda **kw: real(windows=windows, **kw))
            with pytest.raises(values.ValuesError, match="could not run"):
                values.models("muse")
        assert seen[0].get("start_new_session") is True
        assert seen[1]["creationflags"] == 0x08000000 | 0x00000200
        assert "start_new_session" not in seen[1]

    def test_muse_cleanup_goes_through_the_shared_group_termination(self, tmp_path, monkeypatch):
        self._fake_muse(tmp_path, monkeypatch, self.MUSE_OK)
        called = []
        real = values.terminate_group
        def cleanup(proc, **kwargs):
            assert proc.returncode is None, "leader reaped before group ownership was released"
            called.append(proc.pid)
            return real(proc, **kwargs)
        monkeypatch.setattr(values, "terminate_group", cleanup)
        values.models("muse")
        assert len(called) == 1

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
        assert values.verbs("muse") == ["efforts", "models"]

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
