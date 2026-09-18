from __future__ import annotations

import json

import pytest

import dispatch
import rig
from engines import run as engine_run
from r4t import main
from test_rig_run import calls_of, drive_preset


@pytest.mark.parametrize(
    "preset,flag,anchor",
    [
        ("claude", "--effort", None),
        ("copilot", "--reasoning-effort", None),
        ("muse", "--reasoning-effort", "exec"),
        ("agy", "--effort", None),
        ("opencode", "--variant", "run"),
        ("codex", "-c", "exec"),
        ("ollama-claude", "--effort", "--"),
        ("ollama-copilot", "--reasoning-effort", "--"),
        ("ollama-opencode", "--variant", "run"),
        ("ollama-codex", "-c", "exec"),
    ],
)
def test_effort_reaches_child_cli(preset, flag, anchor):
    model = None if preset == "agy" else "test-model"
    argv = rig.build_preset_invoke(preset, model=model, effort="low")
    at = argv.index(flag)
    assert at == (argv.index(anchor) + 1 if anchor else 1)
    assert argv[at + 1] == ("model_reasoning_effort=low" if flag == "-c" else "low")
    assert argv[-1] == "{prompt}"
    if preset.startswith("ollama-"):
        assert at > argv.index("--")
        assert argv[argv.index("--model") + 1] == "test-model"


@pytest.mark.parametrize("preset", list(rig.HARNESS_PRESETS))
def test_unset_effort_preserves_argv_without_mutation(preset):
    argv = rig.build_preset_invoke(preset, model="test-model")
    before = list(argv)
    assert rig.apply_effort(argv, preset, None) == before
    assert rig.build_preset_invoke(preset, model="test-model", effort=None) == before
    assert argv == before


@pytest.mark.parametrize("preset", ["cursor", "devin", "ollama", None])
def test_unsupported_effort_names_engine(preset):
    with pytest.raises(rig.RigError, match=f"{preset!r} does not support --effort"):
        rig.apply_effort(["cli", "{prompt}"], preset, "low")


@pytest.mark.parametrize(
    "preset", ["claude", "copilot", "muse", "agy", "ollama-claude", "ollama-copilot"]
)
def test_invalid_effort_lists_engine_vocabulary(preset):
    with pytest.raises(rig.RigError, match=f"{preset!r}.*accepted:.*low, medium, high"):
        rig.build_preset_invoke(preset, model="test-model", effort="banana")


@pytest.mark.parametrize(
    "preset", ["opencode", "ollama-opencode", "codex", "ollama-codex"]
)
def test_provider_effort_passthrough_and_empty_rejection(preset):
    argv = rig.build_preset_invoke(preset, model="test-model", effort="provider-depth")
    assert any("provider-depth" in arg for arg in argv)
    for invalid in ("", "   ", 1):
        with pytest.raises(rig.RigError, match="invalid effort"):
            rig.build_preset_invoke(preset, model="test-model", effort=invalid)


def test_muse_accepts_ultra():
    assert "ultra" in rig.build_preset_invoke("muse", effort="ultra")


@pytest.mark.parametrize(
    "argv,preset,expected",
    [
        (
            ["claude", "--effort=high", "-p", "{prompt}"],
            "claude",
            ["claude", "--effort", "low", "-p", "{prompt}"],
        ),
        (
            ["copilot", "--effort", "high", "--reasoning-effort=max", "{prompt}"],
            "copilot",
            ["copilot", "--reasoning-effort", "low", "{prompt}"],
        ),
        (
            [
                "codex",
                "exec",
                "-c",
                "model_reasoning_effort=high",
                "--config=model_reasoning_effort=max",
                "-c",
                "other=true",
                "{prompt}",
            ],
            "codex",
            [
                "codex",
                "exec",
                "-c",
                "model_reasoning_effort=low",
                "-c",
                "other=true",
                "{prompt}",
            ],
        ),
        (
            [
                "ollama",
                "launch",
                "claude",
                "--model",
                "local",
                "--",
                "--effort",
                "max",
                "-p",
                "{prompt}",
            ],
            "ollama-claude",
            [
                "ollama",
                "launch",
                "claude",
                "--model",
                "local",
                "--",
                "--effort",
                "low",
                "-p",
                "{prompt}",
            ],
        ),
    ],
)
def test_override_replaces_existing_effort_and_is_idempotent(argv, preset, expected):
    before = list(argv)
    assert rig.apply_effort(argv, preset, "low") == expected
    assert rig.apply_effort(expected, preset, "low") == expected
    assert argv == before


@pytest.mark.parametrize(
    "preset,argv",
    [
        ("codex", ["codex", "{prompt}"]),
        ("ollama-claude", ["ollama", "launch", "claude", "{prompt}"]),
        ("ollama-codex", ["ollama", "launch", "codex", "--", "{prompt}"]),
    ],
)
def test_broken_stored_argv_fails_closed(preset, argv):
    with pytest.raises(rig.RigError, match="requires"):
        rig.apply_effort(argv, preset, "low")


AGY_NAMES = [
    "Gemini 3.1 Pro (High)",
    "Gemini 3.1 Pro (Low)",
    "Gemini 3.8 Flash (High)",
    "Gemini 3.8 Flash (Medium)",
    "Gemini 3.8 Flash (Low)",
    "Claude Sonnet 4.6 (Thinking)",
]


@pytest.mark.parametrize(
    "query", ["pro", "Gemini 3.1 Pro (High)", "gemini-3.1-pro-high"]
)
def test_agy_explicit_effort_overrides_embedded_suffix(query):
    assert (
        rig.resolve_agy_model(query, names=AGY_NAMES, effort="low")
        == "Gemini 3.1 Pro (Low)"
    )


def test_agy_unavailable_effort_never_switches_family():
    with pytest.raises(
        rig.RigError, match=r"Gemini 3.1 Pro.*medium.*available variants"
    ):
        rig.resolve_agy_model("pro", names=AGY_NAMES, effort="medium")
    with pytest.raises(rig.RigError, match="does not support effort"):
        rig.resolve_agy_model("sonnet", names=AGY_NAMES, effort="high")
    assert rig.resolve_agy_model("pro", names=AGY_NAMES) == "Gemini 3.1 Pro (High)"
    assert rig.resolve_agy_model("pro low", names=AGY_NAMES) == "Gemini 3.1 Pro (Low)"


def test_agy_engine_effort_is_encoded_only_in_model(monkeypatch, tmp_path):
    monkeypatch.setattr(rig, "agy_model_names", lambda *_: AGY_NAMES)
    argv = engine_run.build_argv(
        "agy",
        "--effort=high",
        model="pro high",
        effort="low",
        timeout=900,
        workdir=tmp_path,
    )
    assert argv[argv.index("--model") + 1] == "Gemini 3.1 Pro (Low)"
    assert "--effort" not in argv
    assert argv[-1] == "--effort=high"


def test_agy_pinned_effort_removes_stored_native_flag():
    argv = ["agy", "--effort=high", "--model", "{model}", "--print", "{prompt}"]
    assert rig.apply_effort(argv, "agy", "low") == [
        "agy",
        "--model",
        "{model}",
        "--print",
        "{prompt}",
    ]


def test_agy_unpinned_effort_uses_native_flag(tmp_path):
    argv = engine_run.build_argv(
        "agy", "hi", model=None, effort="low", timeout=900, workdir=tmp_path
    )
    assert "--model" not in argv
    assert argv[argv.index("--effort") + 1] == "low"


def test_rig_effort_persistence_set_unset_and_swap_validation(tmp_path):
    config = tmp_path / "rigs.json"
    rig.add_preset_rig(config, "worker", "muse", effort="ultra")
    assert rig.rig_setting(config, "worker", "effort").value == "ultra"
    raw = json.loads(config.read_text())["worker"]
    assert "--reasoning-effort" not in raw["invoke"]
    before = config.read_bytes()
    with pytest.raises(rig.RigError, match="invalid effort"):
        rig.swap_preset_rig(config, "worker", "claude")
    assert config.read_bytes() == before
    rig.swap_preset_rig(config, "worker", "claude", effort="low")
    assert rig.rig_setting(config, "worker", "effort").value == "low"
    with pytest.raises(rig.RigError, match="does not support"):
        rig.swap_preset_rig(config, "worker", "cursor")
    rig.set_rig_value(config, "worker", "effort", "high")
    loaded = rig.load_rig_config(config).rigs["worker"]
    assert loaded.effort == "high"
    assert loaded.argv("hi")[1:3] == ["--effort", "high"]
    rig.set_rig_model(config, "worker", "sonnet")
    assert rig.rig_setting(config, "worker", "effort").value == "high"
    rig.unset_rig_value(config, "worker", "effort")
    assert rig.rig_setting(config, "worker", "effort").value is None
    assert "--effort" not in rig.load_rig_config(config).rigs["worker"].argv("hi")
    rig.swap_preset_rig(config, "worker", "cursor")


@pytest.mark.parametrize("effort", ["banana", "", 1])
def test_hand_edited_invalid_rig_fails_at_load(tmp_path, effort):
    config = tmp_path / "rigs.json"
    config.write_text(
        json.dumps(
            {
                "worker": {
                    "preset": "claude",
                    "invoke": ["claude", "{prompt}"],
                    "effort": effort,
                }
            }
        )
    )
    assert "effort" in rig.load_rig_config(config).rigs["worker"].error


def test_rig_run_override_is_transient_and_engine_run_threads_flag(
    monkeypatch, tmp_path
):
    calls = drive_preset(monkeypatch, tmp_path, "claude")
    config = tmp_path / "rigs.json"
    assert (
        main(
            [
                "rig",
                "add",
                "worker",
                "claude",
                "--effort",
                "high",
                "--rig-config",
                str(config),
            ]
        )
        == 0
    )
    before = config.read_bytes()
    for extra, expected in [([], "high"), (["--effort", "low"], "low")]:
        assert (
            main(
                [
                    "rig",
                    "run",
                    "worker",
                    "hello",
                    "--dir",
                    str(tmp_path),
                    "--rig-config",
                    str(config),
                    "--no-scaffold",
                    *extra,
                ]
            )
            == 0
        )
        argv = calls_of(calls)[-1]["argv"]
        assert argv[argv.index("--effort") + 1] == expected
    assert config.read_bytes() == before
    assert (
        main(
            [
                "engine",
                "claude",
                "run",
                "hello",
                "--dir",
                str(tmp_path),
                "--no-scaffold",
                "--effort",
                "medium",
            ]
        )
        == 0
    )
    argv = calls_of(calls)[-1]["argv"]
    assert argv[argv.index("--effort") + 1] == "medium"


def test_invalid_effort_never_spawns_or_charges(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(engine_run, "_spawn", lambda *a: calls.append("spawn"))
    with pytest.raises(engine_run.RunError, match="invalid effort"):
        engine_run.execute(
            "claude",
            "hello",
            dir_path=tmp_path,
            model=None,
            effort="banana",
            agent=None,
            timeout=900,
            scaffold=False,
            charge_hook=lambda: calls.append("charge"),
        )
    assert calls == []


def test_dispatch_and_distill_apply_effort(monkeypatch, tmp_path):
    calls = drive_preset(monkeypatch, tmp_path, "agy")
    monkeypatch.setattr(rig, "agy_model_names", lambda *_: AGY_NAMES)
    worker = rig.Rig(
        name="worker",
        preset="agy",
        invoke=rig.build_preset_invoke("agy", model="pro high"),
        model="pro high",
        model_resolver="agy-live",
        effort="low",
    )
    code, _, _, _ = dispatch.run_harness(worker, "hello", tmp_path)
    assert code == 0
    argv = calls_of(calls)[0]["argv"]
    assert argv[argv.index("--model") + 1] == "Gemini 3.1 Pro (Low)"
    assert "--effort" not in argv
    command = worker.distill_command(tmp_path)
    assert "--effort" not in command
    assert "Gemini 3.1 Pro (Low)" in command
    worker.effort = "medium"
    code, out, _, _ = dispatch.run_harness(worker, "hello", tmp_path)
    assert code == 127 and "available variants" in out
    assert len(calls_of(calls)) == 1
    assert worker.distill_command(tmp_path) is None


def test_cursor_model_options_remain_verbatim(tmp_path):
    model = "claude-opus-4-8[context=1m,effort=high,fast=false]"
    argv = engine_run.build_argv(
        "cursor", "hi", model=model, timeout=900, workdir=tmp_path
    )
    assert argv[argv.index("--model") + 1] == model
    with pytest.raises(engine_run.RunError, match="use --model"):
        engine_run.build_argv(
            "cursor", "hi", model=model, effort="low", timeout=900, workdir=tmp_path
        )


def test_codex_resume_keeps_effort_after_subcommand(tmp_path):
    argv = engine_run.build_argv(
        "codex",
        "hi",
        model="test-model",
        effort="low",
        timeout=900,
        workdir=tmp_path,
        continue_conversation=True,
    )
    assert (
        argv.index("exec")
        < argv.index("resume")
        < argv.index("model_reasoning_effort=low")
    )


def test_effort_preserves_arguments_after_child_separator():
    argv = [
        "ollama",
        "launch",
        "claude",
        "--model",
        "local",
        "--",
        "-p",
        "--",
        "--effort=high {prompt}",
    ]
    out = rig.apply_effort(argv, "ollama-claude", "low")
    assert out[6:] == ["--effort", "low", "-p", "--", "--effort=high {prompt}"]


def test_wrapper_effort_without_anchor_stays_in_child_argv(monkeypatch):
    entry = dict(rig.HARNESS_PRESETS["ollama-claude"])
    entry.pop("effort_anchor")
    monkeypatch.setitem(rig.HARNESS_PRESETS, "ollama-claude", entry)
    argv = ["ollama", "launch", "claude", "--model", "local", "--", "{prompt}"]
    assert rig.apply_effort(argv, "ollama-claude", "low") == [
        *argv[:6],
        "--effort",
        "low",
        "{prompt}",
    ]


def test_codex_compact_config_override_is_replaced():
    argv = [
        "codex",
        "exec",
        "-cmodel_reasoning_effort=high",
        "-c=other=true",
        "{prompt}",
    ]
    assert rig.apply_effort(argv, "codex", "low") == [
        "codex",
        "exec",
        "-c",
        "model_reasoning_effort=low",
        "-c=other=true",
        "{prompt}",
    ]
