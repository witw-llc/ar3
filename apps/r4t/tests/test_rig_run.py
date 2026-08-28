"""`r4t rig run` — one headless turn as a named rig.

The engine layer is bare metal; this layer is the engine plus what the rig has
been tuned to plus the budget gate. These tests drive the CLI end to end
against a recording stand-in CLI spliced into the preset table, so what is
asserted is the argv, environment and bucket a real turn would get.
"""
from __future__ import annotations

import json
import sys
import textwrap
import time
from pathlib import Path

import pytest

import state
from conftest import write_path_executable
from engines import run as engine_run
from r4t import main as r4t_main


def recorder(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """A stand-in CLI that records its argv and a probe variable per call.

    It must be executable in its own right rather than run as
    `python <script>`, because a preset's binary is argv[0] and r4t splices
    `--model` and its own unattended-turn flags immediately after it — putting
    the interpreter in argv[0] would break the splice these tests exist to
    check. `write_path_executable` keeps that property on every platform: the
    launcher it writes on Windows carries the recorder's own name, so argv[0]
    is still the CLI and the flags still land where the preset says.
    """
    calls = tmp_path / f"{name}-calls"
    calls.mkdir(exist_ok=True)
    script = write_path_executable(
        tmp_path,
        f"{name}-recorder",
        textwrap.dedent(
            f"""\
            import json, os, sys
            calls_dir = {str(calls)!r}
            n = len(os.listdir(calls_dir))
            with open(os.path.join(calls_dir, f"call-{{n:03d}}.json"), "w", encoding="utf-8", newline="") as f:
                json.dump(
                    {{"argv": sys.argv[1:], "knob": os.environ.get("RIG_KNOB", "")}}, f
                )
            print("recorder ran")
            """
        ),
    )
    return script, calls


def drive_preset(monkeypatch, tmp_path, name: str) -> Path:
    """Point one preset's binary at the recorder and keep every other token
    the real preset carries, so the permission and allowlist translations are
    exercised against the argv they were written for."""
    import rig as rig_module

    script, calls = recorder(tmp_path, name)
    real = rig_module.HARNESS_PRESETS[name]
    monkeypatch.setitem(
        rig_module.HARNESS_PRESETS,
        name,
        {**real, "invoke": [str(script), *real["invoke"][1:]]},
    )
    return calls


@pytest.fixture
def preset(monkeypatch, tmp_path):
    """The `claude` preset, driven by the recorder instead of the real CLI."""
    return drive_preset(monkeypatch, tmp_path, "claude")


def write_rigs(tmp_path: Path, entry: dict, name: str = "cheap") -> Path:
    path = tmp_path / "rigs.json"
    path.write_text(json.dumps({name: entry}), encoding="utf-8")
    return path


def claude_rig(**extra) -> dict:
    return {"preset": "claude", "invoke": ["claude", "{prompt}"], **extra}


def rig_cli(*args) -> int:
    return r4t_main(["rig", "run", *args])


def run_rig(tmp_path, config, *extra, name="cheap", prompt="do the thing"):
    args = [name, "--rig-config", str(config), "--dir", str(tmp_path), *extra]
    if prompt is not None:
        args.append(prompt)
    return rig_cli(*args)


def calls_of(calls: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(calls.iterdir())]


def only_call(calls: Path) -> dict:
    [call] = calls_of(calls)
    return call


def budgeted(**extra) -> dict:
    return claude_rig(rig_budget_max=4, rig_budget_earn_per_hour=20, **extra)


class FakeClock:
    """Stands in for `r4t`'s own `time` module, so a `--wait` poll can be
    driven without patching the real `time.sleep` that `subprocess.wait`
    (and everything else) also calls."""

    def __init__(self, on_sleep=None):
        self.slept: list[float] = []
        self._on_sleep = on_sleep

    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self._on_sleep is not None:
            self._on_sleep()


def refill_bucket(rig: str = "cheap", level: float = 4.0) -> None:
    """Stand in for the wall clock inside a `--wait` poll. The bucket is
    machine-global, so a level that rises between two polls is exactly what a
    real wait observes — from elapsed time, or from another node's turn."""
    state.atomic_write_json(
        state.rig_buckets_path(), {rig: {"level": level, "at": time.time()}}
    )


class TestRigResolution:
    def test_unknown_rig_names_presets_and_add(self, r4t_home, tmp_path, capsys):
        config = write_rigs(tmp_path, claude_rig())
        assert run_rig(tmp_path, config, name="nope") == 1
        err = capsys.readouterr().err
        assert "r4t rig presets" in err
        assert "r4t rig add nope" in err

    def test_missing_config_is_the_same_refusal(self, r4t_home, tmp_path, capsys):
        assert run_rig(tmp_path, tmp_path / "absent.json") == 1
        assert "not found" in capsys.readouterr().err

    def test_rig_with_no_preset_points_at_swap(self, r4t_home, tmp_path, capsys):
        config = write_rigs(tmp_path, {"invoke": ["something", "{prompt}"]})
        assert run_rig(tmp_path, config) == 1
        err = capsys.readouterr().err
        assert "has no preset" in err
        assert "r4t rig swap cheap" in err

    def test_non_run_preset_names_the_engines_that_can(
        self, r4t_home, tmp_path, capsys
    ):
        config = write_rigs(
            tmp_path, {"preset": "ollama", "invoke": ["ollama", "run", "{prompt}"]}
        )
        assert run_rig(tmp_path, config) == 1
        err = capsys.readouterr().err
        assert "does not support run" in err
        for engine in sorted(engine_run.RUN_ENGINES):
            assert engine in err

    def test_invalid_rig_fails_closed_with_its_error(self, r4t_home, tmp_path, capsys):
        config = write_rigs(tmp_path, claude_rig(rig_budget_max=4))
        assert run_rig(tmp_path, config) == 1
        assert "is invalid" in capsys.readouterr().err


class TestRigFieldsReachTheArgv:
    def test_model_baked_into_the_invoke_is_recovered(
        self, r4t_home, tmp_path, preset
    ):
        config = write_rigs(
            tmp_path, claude_rig(invoke=["claude", "--model", "haiku", "{prompt}"])
        )
        assert run_rig(tmp_path, config, "--no-scaffold") == 0
        argv = only_call(preset)["argv"]
        assert argv[argv.index("--model") + 1] == "haiku"

    def test_a_live_resolver_rigs_model_setting_resolves_per_turn(
        self, r4t_home, tmp_path, monkeypatch
    ):
        # agy is the one preset that records `model` as a setting, because its
        # display names drift and the friendly string is resolved every turn.
        calls = drive_preset(monkeypatch, tmp_path, "agy")
        monkeypatch.setattr(
            engine_run, "resolve_agy_model", lambda query, **kw: f"Gemini {query}"
        )
        config = write_rigs(
            tmp_path,
            {
                "preset": "agy",
                "invoke": ["agy", "{prompt}"],
                "model": "flash",
                "model_resolver": "agy-live",
            },
        )
        assert run_rig(tmp_path, config, "--no-scaffold") == 0
        argv = only_call(calls)["argv"]
        assert argv[argv.index("--model") + 1] == "Gemini flash"

    def test_model_flag_beats_the_rig(self, r4t_home, tmp_path, preset):
        config = write_rigs(
            tmp_path, claude_rig(invoke=["claude", "--model", "haiku", "{prompt}"])
        )
        assert run_rig(tmp_path, config, "--no-scaffold", "--model", "sonnet") == 0
        argv = only_call(preset)["argv"]
        assert argv[argv.index("--model") + 1] == "sonnet"
        assert "haiku" not in argv

    def test_no_model_anywhere_leaves_the_preset_alone(
        self, r4t_home, tmp_path, preset
    ):
        config = write_rigs(tmp_path, claude_rig())
        assert run_rig(tmp_path, config, "--no-scaffold") == 0
        assert "--model" not in only_call(preset)["argv"]

    def test_rig_permissions_reach_the_argv(self, r4t_home, tmp_path, preset):
        config = write_rigs(tmp_path, claude_rig(permissions="ask"))
        assert run_rig(tmp_path, config, "--no-scaffold") == 0
        argv = only_call(preset)["argv"]
        assert "--permission-mode" not in argv
        assert "--allowedTools" not in argv

    def test_permissions_flag_beats_the_rig(self, r4t_home, tmp_path, preset):
        config = write_rigs(tmp_path, claude_rig(permissions="ask"))
        code = run_rig(tmp_path, config, "--no-scaffold", "--permissions", "bypass")
        assert code == 0
        argv = only_call(preset)["argv"]
        assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"

    def test_rig_allowed_tools_replace_the_presets_list(
        self, r4t_home, tmp_path, preset
    ):
        config = write_rigs(tmp_path, claude_rig(allowed_tools="Read Edit"))
        assert run_rig(tmp_path, config, "--no-scaffold") == 0
        argv = only_call(preset)["argv"]
        assert argv[argv.index("--allowedTools") + 1] == "Read Edit"

    def test_allowed_tools_flag_beats_the_rig(self, r4t_home, tmp_path, preset):
        config = write_rigs(tmp_path, claude_rig(allowed_tools="Read Edit"))
        code = run_rig(
            tmp_path, config, "--no-scaffold", "--allowed-tools", "Bash(git:*)"
        )
        assert code == 0
        argv = only_call(preset)["argv"]
        assert argv[argv.index("--allowedTools") + 1] == "Bash(git:*)"

    def test_rig_env_map_reaches_the_turn(self, r4t_home, tmp_path, preset):
        config = write_rigs(tmp_path, claude_rig(env={"RIG_KNOB": "on"}))
        assert run_rig(tmp_path, config, "--no-scaffold") == 0
        assert only_call(preset)["knob"] == "on"

    def test_no_env_map_leaves_the_environment_inherited(
        self, r4t_home, tmp_path, preset, monkeypatch
    ):
        monkeypatch.setenv("RIG_KNOB", "inherited")
        config = write_rigs(tmp_path, claude_rig())
        assert run_rig(tmp_path, config, "--no-scaffold") == 0
        assert only_call(preset)["knob"] == "inherited"

    def test_rig_timeout_reaches_the_engines_own_flag(
        self, r4t_home, tmp_path, monkeypatch
    ):
        # agy is the one preset whose argv states the turn timeout, so it is
        # where a rig's `timeout_seconds` becomes visible.
        calls = drive_preset(monkeypatch, tmp_path, "agy")
        config = write_rigs(
            tmp_path,
            {"preset": "agy", "invoke": ["agy", "{prompt}"], "timeout_seconds": 42},
        )
        assert run_rig(tmp_path, config, "--no-scaffold") == 0
        argv = only_call(calls)["argv"]
        assert argv[argv.index("--print-timeout") + 1] == "42s"

    def test_timeout_flag_beats_the_rig(self, r4t_home, tmp_path, monkeypatch):
        calls = drive_preset(monkeypatch, tmp_path, "agy")
        config = write_rigs(
            tmp_path,
            {"preset": "agy", "invoke": ["agy", "{prompt}"], "timeout_seconds": 42},
        )
        assert run_rig(tmp_path, config, "--no-scaffold", "--timeout", "77") == 0
        argv = only_call(calls)["argv"]
        assert argv[argv.index("--print-timeout") + 1] == "77s"


class TestEngineFlagsPassThrough:
    def test_scaffold_is_on_by_default(self, r4t_home, tmp_path, preset):
        config = write_rigs(tmp_path, claude_rig())
        assert run_rig(tmp_path, config) == 0
        prompt = only_call(preset)["argv"][-1]
        assert prompt.startswith("Smart cold boot:")
        assert prompt.endswith("do the thing")

    def test_no_scaffold_sends_the_prompt_untouched(self, r4t_home, tmp_path, preset):
        config = write_rigs(tmp_path, claude_rig())
        assert run_rig(tmp_path, config, "--no-scaffold") == 0
        assert only_call(preset)["argv"][-1] == "do the thing"

    def test_agent_adds_the_convo_step(self, r4t_home, tmp_path, preset):
        config = write_rigs(tmp_path, claude_rig())
        assert run_rig(tmp_path, config, "--agent", "ares") == 0
        assert "a8s convo ares" in only_call(preset)["argv"][-1]

    def test_continue_splices_the_presets_own_tokens(self, r4t_home, tmp_path, preset):
        config = write_rigs(tmp_path, claude_rig())
        assert run_rig(tmp_path, config, "--no-scaffold", "--continue") == 0
        assert "--continue" in only_call(preset)["argv"]

    def test_stdin_dash_reads_the_prompt(self, r4t_home, tmp_path, preset, monkeypatch):
        import io

        monkeypatch.setattr(sys, "stdin", io.StringIO("piped in"))
        config = write_rigs(tmp_path, claude_rig())
        assert run_rig(tmp_path, config, "--no-scaffold", prompt="-") == 0
        assert only_call(preset)["argv"][-1] == "piped in"

    def test_echo_prints_the_composed_argv(self, r4t_home, tmp_path, preset, capsys):
        config = write_rigs(tmp_path, claude_rig(permissions="bypass"))
        assert run_rig(tmp_path, config, "--no-scaffold", "--echo") == 0
        err = capsys.readouterr().err
        assert "r4t engine echo: argv:" in err
        assert "bypassPermissions" in err

    def test_lessons_rotate_at_the_flags_cap(self, r4t_home, tmp_path, preset):
        (tmp_path / "LESSONS.md").write_text(
            "".join(f"- lesson {i}\n" for i in range(5)), encoding="utf-8"
        )
        config = write_rigs(tmp_path, claude_rig())
        assert run_rig(tmp_path, config, "--lessons-cap", "3") == 0
        assert (tmp_path / "LESSONS.md").read_text().count("\n") == 3
        assert (tmp_path / "LESSONS-ARCHIVE.md").is_file()

    def test_prompt_required_without_idle(self, r4t_home, tmp_path, capsys):
        config = write_rigs(tmp_path, claude_rig())
        assert run_rig(tmp_path, config, prompt=None) == 2
        assert "PROMPT is required" in capsys.readouterr().err

    def test_idle_latch_skips_the_second_turn(self, r4t_home, tmp_path, preset):
        config = write_rigs(tmp_path, claude_rig())
        assert run_rig(tmp_path, config, "--idle", prompt=None) == 0
        assert len(calls_of(preset)) == 1
        assert run_rig(tmp_path, config, "--idle", prompt=None) == 0
        assert len(calls_of(preset)) == 1

    def test_idle_and_continue_contradict(self, r4t_home, tmp_path, capsys):
        config = write_rigs(tmp_path, claude_rig())
        assert run_rig(tmp_path, config, "--idle", "--continue", prompt=None) == 2
        assert "cold start" in capsys.readouterr().err

    def test_wait_and_now_contradict(self, r4t_home, tmp_path, capsys):
        config = write_rigs(tmp_path, claude_rig())
        assert run_rig(tmp_path, config, "--wait", "--now") == 2
        assert "contradict" in capsys.readouterr().err


class TestBudgetGate:
    def test_a_rig_with_no_budget_runs_with_no_bucket_at_all(
        self, r4t_home, tmp_path, preset
    ):
        config = write_rigs(tmp_path, claude_rig())
        assert run_rig(tmp_path, config, "--no-scaffold") == 0
        assert len(calls_of(preset)) == 1
        assert not state.rig_buckets_path().exists()

    def test_a_full_bucket_runs_and_pays_one_unit(self, r4t_home, tmp_path, preset):
        config = write_rigs(tmp_path, budgeted())
        assert run_rig(tmp_path, config, "--no-scaffold") == 0
        assert len(calls_of(preset)) == 1
        assert state.rig_budget_level("cheap", 4.0, 20.0) == pytest.approx(3.0, abs=0.1)

    def test_an_empty_bucket_refuses_and_names_both_flags(
        self, r4t_home, tmp_path, preset, capsys
    ):
        config = write_rigs(tmp_path, budgeted())
        state.rig_budget_drain("cheap")
        assert run_rig(tmp_path, config, "--no-scaffold") == 1
        err = capsys.readouterr().err
        assert "is resting" in err
        assert "--wait" in err and "--now" in err
        assert "~3 min" in err  # 1 unit at 20/hour
        assert calls_of(preset) == []

    def test_a_refusal_spends_nothing(self, r4t_home, tmp_path, preset):
        config = write_rigs(tmp_path, budgeted())
        state.rig_budget_drain("cheap")
        assert run_rig(tmp_path, config, "--no-scaffold") == 1
        assert state.rig_budget_level("cheap", 4.0, 0.0) == 0.0

    def test_a_turn_refused_at_composition_spends_nothing(
        self, r4t_home, tmp_path, monkeypatch, capsys
    ):
        """A bad per-run override errors before any spawn and must not pay:
        the charge fires inside execute, immediately before the spawn. Found
        in review — a codex rig with `--permissions ask` (below codex's
        floor) exited 1 and still spent the last unit."""
        calls = drive_preset(monkeypatch, tmp_path, "codex")
        config = write_rigs(
            tmp_path,
            {
                "preset": "codex",
                "invoke": ["codex", "exec", "{prompt}"],
                "rig_budget_max": 4,
                "rig_budget_earn_per_hour": 20,
            },
        )
        assert run_rig(
            tmp_path, config, "--no-scaffold", "--permissions", "ask"
        ) != 0
        assert calls_of(calls) == []
        assert state.rig_budget_level("cheap", 4.0, 0.0) == pytest.approx(4.0)
        assert "codex" in capsys.readouterr().err

    def test_now_runs_anyway_and_still_pays_down_to_the_floor(
        self, r4t_home, tmp_path, preset
    ):
        config = write_rigs(tmp_path, budgeted())
        state.rig_budget_drain("cheap")
        assert run_rig(tmp_path, config, "--no-scaffold", "--now") == 0
        assert len(calls_of(preset)) == 1
        # The charge clamps at zero: the bucket rests at its floor, never below.
        assert state.rig_budget_level("cheap", 4.0, 0.0) == 0.0

    def test_wait_holds_then_runs(self, r4t_home, tmp_path, preset, monkeypatch, capsys):
        import r4t as r4t_module

        config = write_rigs(tmp_path, budgeted())
        state.rig_budget_drain("cheap")
        clock = FakeClock(refill_bucket)
        monkeypatch.setattr(r4t_module, "time", clock)
        assert run_rig(tmp_path, config, "--no-scaffold", "--wait") == 0
        assert len(calls_of(preset)) == 1
        assert clock.slept == [pytest.approx(5.0, abs=0.01)]
        err = capsys.readouterr().err
        assert err.count("waiting") == 1
        assert "one turn's budget" in err

    def test_wait_on_a_full_bucket_says_nothing_and_runs(
        self, r4t_home, tmp_path, preset, monkeypatch, capsys
    ):
        import r4t as r4t_module

        clock = FakeClock()
        monkeypatch.setattr(r4t_module, "time", clock)
        config = write_rigs(tmp_path, budgeted())
        assert run_rig(tmp_path, config, "--no-scaffold", "--wait") == 0
        assert clock.slept == []
        assert "waiting" not in capsys.readouterr().err

    def test_a_latched_idle_pass_spends_nothing(self, r4t_home, tmp_path, preset):
        config = write_rigs(tmp_path, budgeted())
        (tmp_path / engine_run.IDLE_MARKER_NAME).touch()
        assert run_rig(tmp_path, config, "--idle", prompt=None) == 0
        assert calls_of(preset) == []
        assert not state.rig_buckets_path().exists()

    def test_the_bucket_is_machine_global_not_per_roster(
        self, r4t_home, tmp_path, preset
    ):
        config = write_rigs(tmp_path, budgeted())
        assert run_rig(tmp_path, config, "--no-scaffold") == 0
        # Outside any roster dir: one subscription, every roster on the machine.
        assert state.rig_buckets_path().is_relative_to(r4t_home)
        assert state.rig_buckets_path().name == "rig-buckets.json"
        assert "cheap" in json.loads(state.rig_buckets_path().read_text())


class TestJsonReport:
    def test_a_ran_turn_reports_the_engine_and_the_budget(
        self, r4t_home, tmp_path, preset, capsys
    ):
        config = write_rigs(tmp_path, budgeted())
        assert run_rig(tmp_path, config, "--no-scaffold", "--json") == 0
        report = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        assert report["rig"] == "cheap"
        assert report["engine"] == "claude"
        assert report["dir"] == str(tmp_path)
        assert report["ran"] is True
        assert report["reason"] == "ran"
        assert report["exit_code"] == 0
        assert report["budget"]["level_before"] == pytest.approx(4.0, abs=0.1)
        assert report["budget"]["level_after"] == pytest.approx(3.0, abs=0.1)
        assert report["budget"]["waited_seconds"] == 0.0
        assert report["budget"]["forced"] is False

    def test_an_unbudgeted_rig_reports_a_null_budget(
        self, r4t_home, tmp_path, preset, capsys
    ):
        config = write_rigs(tmp_path, claude_rig())
        assert run_rig(tmp_path, config, "--no-scaffold", "--json") == 0
        report = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        assert report["budget"] is None
        assert report["ran"] is True

    def test_a_refusal_reports_the_wait(self, r4t_home, tmp_path, preset, capsys):
        config = write_rigs(tmp_path, budgeted())
        state.rig_budget_drain("cheap")
        assert run_rig(tmp_path, config, "--no-scaffold", "--json") == 1
        report = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        assert report["ran"] is False
        assert report["reason"] == "resting"
        assert report["exit_code"] == 1
        assert report["budget"]["seconds_until"] == pytest.approx(180.0, abs=2.0)
        assert report["budget"]["level_after"] == pytest.approx(0.0, abs=0.1)

    def test_now_reports_itself_as_forced(self, r4t_home, tmp_path, preset, capsys):
        config = write_rigs(tmp_path, budgeted())
        state.rig_budget_drain("cheap")
        assert run_rig(tmp_path, config, "--no-scaffold", "--json", "--now") == 0
        report = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        assert report["budget"]["forced"] is True
        assert report["ran"] is True

    def test_a_waited_turn_reports_the_seconds(
        self, r4t_home, tmp_path, preset, monkeypatch, capsys
    ):
        import r4t as r4t_module

        config = write_rigs(tmp_path, budgeted())
        state.rig_budget_drain("cheap")
        monkeypatch.setattr(r4t_module, "time", FakeClock(refill_bucket))
        assert run_rig(tmp_path, config, "--no-scaffold", "--json", "--wait") == 0
        report = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        assert report["budget"]["waited_seconds"] >= 0.0
        assert report["ran"] is True

    def test_a_latched_idle_pass_reports_the_reason(
        self, r4t_home, tmp_path, preset, capsys
    ):
        config = write_rigs(tmp_path, claude_rig())
        (tmp_path / engine_run.IDLE_MARKER_NAME).touch()
        assert run_rig(tmp_path, config, "--idle", "--json", prompt=None) == 0
        report = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        assert report["ran"] is False
        assert report["reason"] == "idle-latched"

    def test_the_report_never_lands_on_stdout(
        self, r4t_home, tmp_path, preset, capsys
    ):
        config = write_rigs(tmp_path, budgeted())
        assert run_rig(tmp_path, config, "--no-scaffold", "--json") == 0
        out = capsys.readouterr().out
        assert "\"rig\"" not in out
