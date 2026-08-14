from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

import rig as rig_module
from rig import (
    Rig,
    CONFIGURABLE_RIG_KEYS,
    DEFAULT_BUDGET_EARN_PER_HOUR,
    DEFAULT_ECHO_MAX_CHARS,
    DEFAULT_BUDGET_MAX,
    DEFAULT_CONCURRENCY,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_MAX_SENDS_PER_TURN,
    DEFAULT_MIN_SECONDS_BETWEEN_TURN_STARTS,
    DEFAULT_CELL_BUDGET_EARN_PER_HOUR,
    DEFAULT_CELL_BUDGET_MAX,
    DEFAULT_TIMEOUT_SECONDS,
    HARNESS_PRESETS,
    KNOWLEDGE_TIER_HIGH,
    KNOWLEDGE_TIER_LOW,
    KNOWLEDGE_TIER_MID,
    RigError,
    add_preset_rig,
    apply_mcp,
    build_preset_invoke,
    continue_collisions,
    continue_presets,
    default_config_payload,
    format_preset_invoke,
    fuzzy_match_model,
    is_below_knowledge_floor,
    knowledge_tier_bytes,
    load_rig_config,
    mcp_presets,
    preset_names,
    remove_rig,
    resolve_agy_model,
    resolve_framing,
    resolve_knowledge_bytes,
    rig_setting,
    rig_settings,
    set_rig_value,
    swap_preset_rig,
    unset_rig_value,
)
from roster import (
    KNOWLEDGE_DEFAULT_BUDGET,
    KNOWLEDGE_SIZES,
    FramingSpec,
    Member,
    parse_roster,
)
from r4t import main as r4t_main


def write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "rigs.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def member(name="Phil", rig="junior-dev") -> Member:
    return Member(name=name, rig=rig)


class TestLoading:
    def test_rigs_and_defaults(self, tmp_path):
        config = load_rig_config(
            write_config(tmp_path, {"fast": {"invoke": ["run", "{prompt}"]}})
        )
        rig = config.rigs["fast"]
        assert rig.invoke == ["run", "{prompt}"]
        assert rig.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
        assert rig.concurrency == DEFAULT_CONCURRENCY
        assert rig.max_sends_per_turn == DEFAULT_MAX_SENDS_PER_TURN
        assert rig.budget_max == DEFAULT_BUDGET_MAX == 8.0
        assert rig.budget_earn_per_hour == DEFAULT_BUDGET_EARN_PER_HOUR == 4.0

    def test_zero_config_gets_full_protection(self, tmp_path):
        config = load_rig_config(
            write_config(tmp_path, {"t": {"invoke": ["x", "{prompt}"]}})
        )
        assert config.throttle.max_concurrent == DEFAULT_MAX_CONCURRENT == 1
        assert (
            config.throttle.min_seconds_between_turn_starts
            == DEFAULT_MIN_SECONDS_BETWEEN_TURN_STARTS
            == 15.0
        )
        assert config.cell_budget_max == DEFAULT_CELL_BUDGET_MAX == 16.0
        assert (
            config.cell_budget_earn_per_hour == DEFAULT_CELL_BUDGET_EARN_PER_HOUR == 8.0
        )
        assert config.breaker_cap == 5
        assert config.breaker_cooldown_seconds == 600.0
        assert config.quiet_task_seconds == 1800.0
        assert config.log_retention_days == 14

    def test_explicit_limits(self, tmp_path):
        config = load_rig_config(
            write_config(
                tmp_path,
                {
                    "t": {
                        "invoke": ["x", "{prompt}"],
                        "timeout_seconds": 60,
                        "concurrency": 3,
                        "budget_max": 10,
                        "budget_earn_per_hour": 2,
                    }
                },
            )
        )
        rig = config.rigs["t"]
        assert (rig.timeout_seconds, rig.concurrency) == (60, 3)
        assert (rig.budget_max, rig.budget_earn_per_hour) == (10, 2)

    def test_explicit_governance_keys(self, tmp_path):
        config = load_rig_config(
            write_config(
                tmp_path,
                {
                    "t": {"invoke": ["x", "{prompt}"]},
                    "throttle": {"max_concurrent": 0, "min_seconds_between_turn_starts": 0},
                    "cell_budget_max": 4,
                    "cell_budget_earn_per_hour": 2,
                    "breaker_cap": 2,
                    "breaker_cooldown_seconds": 30,
                    "quiet_task_seconds": 60,
                    "log_retention_days": 3,
                },
            )
        )
        assert config.throttle.max_concurrent == 0
        assert config.throttle.min_seconds_between_turn_starts == 0
        assert config.cell_budget_max == 4
        assert config.cell_budget_earn_per_hour == 2
        assert config.breaker_cap == 2
        assert config.breaker_cooldown_seconds == 30
        assert config.quiet_task_seconds == 60
        assert config.log_retention_days == 3

    def test_quiet_task_zero_means_off(self, tmp_path):
        # The sweep has always read <= 0 as disabled while the loader rejected
        # it, so the obvious off switch was a config error — and a config error
        # fails the whole dispatch path, which is an outage (#58).
        config = load_rig_config(
            write_config(
                tmp_path,
                {"t": {"invoke": ["x", "{prompt}"]}, "quiet_task_seconds": 0},
            )
        )
        assert config.quiet_task_seconds == 0

    def test_log_retention_zero_means_keep_forever(self, tmp_path):
        config = load_rig_config(
            write_config(
                tmp_path,
                {"t": {"invoke": ["x", "{prompt}"]}, "log_retention_days": 0},
            )
        )
        assert config.log_retention_days == 0

    def test_negative_log_retention_raises(self, tmp_path):
        with pytest.raises(RigError):
            load_rig_config(
                write_config(
                    tmp_path,
                    {"t": {"invoke": ["x", "{prompt}"]}, "log_retention_days": -1},
                )
            )

    def test_bad_governance_values_raise(self, tmp_path):
        for key, value in (
            ("cell_budget_max", 0),
            ("cell_budget_earn_per_hour", -1),
            ("breaker_cap", 0),
            ("breaker_cooldown_seconds", -5),
        ):
            with pytest.raises(RigError):
                load_rig_config(
                    write_config(tmp_path, {"t": {"invoke": ["x", "{prompt}"]}, key: value})
                )

    def test_comment_keys_ignored(self, tmp_path):
        config = load_rig_config(
            write_config(
                tmp_path,
                {
                    "_comment": "hi",
                    "t": {"_comment": "x", "invoke": ["x", "{prompt}"]},
                    "pins": {"_comment": "x", "phil": "t"},
                },
            )
        )
        assert list(config.rigs) == ["t"]
        assert config.pins == {"phil": "t"}

    def test_rig_names_case_insensitive(self, tmp_path):
        config = load_rig_config(
            write_config(tmp_path, {"Leader": {"invoke": ["x", "{prompt}"]}})
        )
        rig, err, _ = config.rig_for(member(rig="leader"))
        assert err is None
        assert rig.name == "leader"

    def test_malformed_json_raises(self, tmp_path):
        path = tmp_path / "rigs.json"
        path.write_text("{nope", encoding="utf-8")
        with pytest.raises(RigError):
            load_rig_config(path)

    def test_non_object_raises(self, tmp_path):
        path = tmp_path / "rigs.json"
        path.write_text("[1,2]", encoding="utf-8")
        with pytest.raises(RigError):
            load_rig_config(path)


class TestFailClosed:
    def test_missing_config_file(self, tmp_path):
        config = load_rig_config(tmp_path / "absent.json")
        assert config.missing
        rig, err, _ = config.rig_for(member())
        assert rig is None
        assert "fail closed" in err

    def test_unknown_rig(self, tmp_path):
        config = load_rig_config(
            write_config(tmp_path, {"other": {"invoke": ["x", "{prompt}"]}})
        )
        rig, err, _ = config.rig_for(member(rig="junior-dev"))
        assert rig is None
        assert "junior-dev" in err and "not found" in err

    def test_invoke_without_prompt_placeholder(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {"t": {"invoke": ["x"]}}))
        rig, err, _ = config.rig_for(member(rig="t"))
        assert rig is None
        assert "{prompt}" in err

    def test_empty_invoke(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {"t": {"invoke": []}}))
        rig, err, _ = config.rig_for(member(rig="t"))
        assert rig is None

    def test_bad_limit_invalidates_rig(self, tmp_path):
        config = load_rig_config(
            write_config(
                tmp_path,
                {"t": {"invoke": ["x", "{prompt}"], "timeout_seconds": -5}},
            )
        )
        rig, err, _ = config.rig_for(member(rig="t"))
        assert rig is None
        assert "timeout_seconds" in err

    def test_member_without_rig(self, tmp_path):
        config = load_rig_config(
            write_config(tmp_path, {"t": {"invoke": ["x", "{prompt}"]}})
        )
        rig, err, _ = config.rig_for(member(rig=None))
        assert rig is None


class TestPins:
    def test_pin_overrides_roster(self, tmp_path):
        config = load_rig_config(
            write_config(
                tmp_path,
                {
                    "cheap": {"invoke": ["c", "{prompt}"]},
                    "fancy": {"invoke": ["f", "{prompt}"]},
                    "pins": {"phil": "cheap"},
                },
            )
        )
        rig, err, pinned = config.rig_for(member(name="Phil", rig="fancy"))
        assert err is None
        assert pinned
        assert rig.name == "cheap"

    def test_pin_is_case_insensitive(self, tmp_path):
        config = load_rig_config(
            write_config(
                tmp_path,
                {"cheap": {"invoke": ["c", "{prompt}"]}, "pins": {"PHIL": "Cheap"}},
            )
        )
        rig, err, pinned = config.rig_for(member(name="phil", rig=None))
        assert err is None and pinned and rig.name == "cheap"

    def test_pin_to_unknown_rig_fails_closed(self, tmp_path):
        config = load_rig_config(
            write_config(
                tmp_path,
                {"cheap": {"invoke": ["c", "{prompt}"]}, "pins": {"phil": "gone"}},
            )
        )
        rig, err, pinned = config.rig_for(member(name="Phil", rig="cheap"))
        assert rig is None and pinned


class TestArgv:
    def test_prompt_substitution_single_element(self, tmp_path):
        config = load_rig_config(
            write_config(tmp_path, {"t": {"invoke": ["run", "-p", "{prompt}"]}})
        )
        argv = config.rigs["t"].argv('hello "world"; rm -rf /')
        assert argv == ["run", "-p", 'hello "world"; rm -rf /']

    def test_embedded_placeholder(self, tmp_path):
        config = load_rig_config(
            write_config(tmp_path, {"t": {"invoke": ["run", "prompt={prompt}"]}})
        )
        assert config.rigs["t"].argv("X") == ["run", "prompt=X"]


class TestWorkdirPlaceholder:
    """`{workdir}` carries the member's resolved `Workdir:` into the argv, for a
    harness that takes its working directory as an argument (#273)."""

    def _rig(self, tmp_path, argv):
        return load_rig_config(write_config(tmp_path, {"t": {"invoke": argv}})).rigs["t"]

    def test_substituted_when_given(self, tmp_path):
        rig = self._rig(tmp_path, ["oc", "--dir", "{workdir}", "{prompt}"])
        assert rig.argv("hi", workdir="/repo/agents/bob") == [
            "oc", "--dir", "/repo/agents/bob", "hi",
        ]

    def test_accepts_a_path(self, tmp_path):
        rig = self._rig(tmp_path, ["oc", "--dir", "{workdir}", "{prompt}"])
        assert rig.argv("hi", workdir=Path("/repo/agents/bob"))[2] == "/repo/agents/bob"

    def test_left_alone_when_not_given(self, tmp_path):
        rig = self._rig(tmp_path, ["oc", "--dir", "{workdir}", "{prompt}"])
        assert rig.argv("hi") == ["oc", "--dir", "{workdir}", "hi"]

    def test_prompt_text_is_never_read_as_a_placeholder(self, tmp_path):
        # A member may write "{workdir}" in a message; the prompt is data.
        rig = self._rig(tmp_path, ["oc", "--dir", "{workdir}", "{prompt}"])
        argv = rig.argv("put it in {workdir}", workdir="/repo/agents/bob")
        assert argv == ["oc", "--dir", "/repo/agents/bob", "put it in {workdir}"]

    def test_embedded_in_a_token(self, tmp_path):
        rig = self._rig(tmp_path, ["oc", "--dir={workdir}", "{prompt}"])
        assert rig.argv("hi", workdir="/w")[1] == "--dir=/w"

    def test_substituted_in_every_pool_variant(self, tmp_path):
        rig = self._rig(tmp_path, [
            ["oc", "--dir", "{workdir}", "{prompt}"],
            ["oc", "--dir", "{workdir}", "-m", "local", "{prompt}"],
        ])
        assert rig.argv("hi", 0, workdir="/w")[2] == "/w"
        assert rig.argv("hi", 1, workdir="/w")[2] == "/w"

    def test_opencode_presets_pin_the_workdir_absolutely(self):
        # opencode resolves a RELATIVE --dir against $PWD (its real cwd only as
        # a fallback), and a spawned harness inherits the PWD of whoever started
        # r4t — so `--dir .` anchored the file tools outside the workdir.
        for preset in ("opencode", "ollama-opencode"):
            argv = HARNESS_PRESETS[preset]["invoke"]
            assert "--dir" in argv
            assert argv[argv.index("--dir") + 1] == "{workdir}"
            assert "." not in argv

    def test_init_starter_rigs_pin_the_workdir(self):
        for entry in default_config_payload().values():
            if not isinstance(entry, dict) or "invoke" not in entry:
                continue
            assert "{workdir}" in entry["invoke"]


class TestContinue:
    def test_only_verified_presets_declare_continue(self):
        # Each of these was verified against the installed CLI's own --help and
        # then live. copilot is absent on purpose: it resumes the machine's most
        # recent session whatever the directory, which no working directory can
        # keep apart (#256).
        assert continue_presets() == [
            "agy", "claude", "codex", "cursor", "ollama-opencode", "opencode",
        ]

    def test_continue_tokens_are_appended_to_the_argv(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {
            "t": {"preset": "claude", "invoke": ["claude", "-p", "{prompt}"]},
        }))
        rig = config.rigs["t"]
        assert rig.supports_continue
        assert rig.argv("hi") == ["claude", "-p", "hi"]
        assert rig.argv("hi", continue_conversation=True) == [
            "claude", "-p", "hi", "--continue",
        ]

    def test_rig_without_preset_cannot_continue(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {"t": {"invoke": ["run", "{prompt}"]}}))
        rig = config.rigs["t"]
        assert rig.supports_continue is False
        assert rig.argv("hi", continue_conversation=True) == ["run", "hi"]

    def test_added_preset_rig_carries_its_preset(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "solo", "cursor")
        rig = load_rig_config(path).rigs["solo"]
        assert rig.preset == "cursor"
        assert rig.argv("hi", continue_conversation=True)[-1] == "--continue"

    def test_no_prior_conversation_pattern_is_per_preset(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {
            "c": {"preset": "cursor", "invoke": ["agent", "-p", "{prompt}"]},
            "k": {"preset": "claude", "invoke": ["claude", "-p", "{prompt}"]},
        }))
        assert config.rigs["c"].had_no_prior_conversation("No previous chats found.")
        assert config.rigs["c"].had_no_prior_conversation("no PREVIOUS chats found")
        assert not config.rigs["c"].had_no_prior_conversation("some other failure")
        # claude founds a conversation silently, so it declares no pattern.
        assert not config.rigs["k"].had_no_prior_conversation("No previous chats found.")

    def test_copilot_is_unsupported_until_sessions_are_pinned(self, tmp_path):
        # Its --continue reaches the machine's most recent session whatever the
        # directory, so no member can be kept apart from another (#256).
        config = load_rig_config(write_config(tmp_path, {
            "t": {"preset": "copilot", "invoke": ["copilot", "-p", "{prompt}"]},
        }))
        rig = config.rigs["t"]
        assert rig.supports_continue is False
        assert rig.argv("hi", continue_conversation=True) == ["copilot", "-p", "hi"]

    def test_cli_key_sees_through_ollama_launch(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {
            "direct": {"preset": "claude", "invoke": ["claude", "-p", "{prompt}"]},
            "local": {
                "preset": "ollama-claude",
                "invoke": ["ollama", "launch", "claude", "-y", "--", "-p", "{prompt}"],
            },
            "bare": {"preset": "ollama", "invoke": ["ollama", "run", "m", "{prompt}"]},
            "custom": {"invoke": ["/opt/bin/agent", "{prompt}"]},
        }))
        assert config.rigs["direct"].cli == "claude"
        assert config.rigs["local"].cli == "claude"
        assert config.rigs["bare"].cli == "ollama"
        assert config.rigs["custom"].cli == "agent"

    def test_launched_opencode_shares_the_opencode_conversation(self, tmp_path):
        # The session store belongs to opencode and is per-directory whether or
        # not `ollama launch` started it, so both rigs collide as one CLI.
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "cloud", "opencode")
        add_preset_rig(path, "local", "ollama-opencode", model="qwen3")
        config = load_rig_config(path)
        assert config.rigs["local"].cli == config.rigs["cloud"].cli == "opencode"
        assert config.rigs["local"].supports_continue
        assert config.rigs["local"].argv("hi", continue_conversation=True)[-1] == "--continue"

    def test_continue_on_unsupported_rig_fails_closed(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {
            "t": {"preset": "copilot", "invoke": ["copilot", "-p", "{prompt}"]},
        }))
        asker = member(name="Ana", rig="t")
        asker.continue_conversation = True
        rig, err, _pinned = config.rig_for(asker)
        assert rig is None
        assert "Continue: on" in err
        assert "claude" in err
        assert "try: r4t rig swap t <preset>" in err

    def test_continue_off_runs_on_any_rig(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {
            "t": {"preset": "copilot", "invoke": ["copilot", "-p", "{prompt}"]},
        }))
        rig, err, _pinned = config.rig_for(member(name="Ana", rig="t"))
        assert rig is not None and err is None

    def test_codex_continue_is_anchored_after_exec(self, tmp_path):
        # `resume --last` is a subcommand: it only reads immediately after
        # `exec`, never appended to a finished argv.
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "codex")
        rig = load_rig_config(path).rigs["worker"]
        assert rig.continue_anchor == "exec"
        assert rig.argv("hi", continue_conversation=True) == [
            "codex", "exec", "resume", "--last",
            "--full-auto", "--skip-git-repo-check", "hi",
        ]

    def test_codex_continue_and_model_splice_in_the_right_order(self, tmp_path):
        # Both anchor on `exec`; the model is spliced at add time and continue
        # at turn time, which lands continue first — the order codex requires.
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "codex", model="gpt-5")
        rig = load_rig_config(path).rigs["worker"]
        assert rig.argv("hi", continue_conversation=True) == [
            "codex", "exec", "resume", "--last", "-m", "gpt-5",
            "--full-auto", "--skip-git-repo-check", "hi",
        ]

    def test_codex_founds_cold_without_a_detection_pattern(self, tmp_path):
        # `exec resume --last` in a virgin directory exits 0 and founds one,
        # so there is no failure to retry (verified live).
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "codex")
        rig = load_rig_config(path).rigs["worker"]
        assert not rig.had_no_prior_conversation("No previous chats found.")

    def test_anchored_preset_without_its_anchor_fails_closed(self, tmp_path):
        # A hand-edited invoke that lost `exec` has nowhere to put the tokens.
        config = load_rig_config(write_config(tmp_path, {
            "t": {"preset": "codex", "invoke": ["codex-wrapper", "{prompt}"]},
        }))
        assert config.rigs["t"].supports_continue is False
        asker = member(name="Ana", rig="t")
        asker.continue_conversation = True
        assert config.rig_for(asker)[0] is None


COLLISION_CONFIG = {
    "solo": {"preset": "claude", "invoke": ["claude", "-p", "{prompt}"]},
    "twin": {"preset": "claude", "invoke": ["claude", "--model", "x", "-p", "{prompt}"]},
    "other": {"preset": "cursor", "invoke": ["agent", "-p", "{prompt}"]},
    "cloud": {"preset": "opencode", "invoke": ["opencode", "run", "--dir", ".", "{prompt}"]},
    "launched": {
        "preset": "ollama-opencode",
        "invoke": ["ollama", "launch", "opencode", "--model", "m", "--", "run", "{prompt}"],
    },
}


class TestContinueCollisions:
    def collisions(self, tmp_path, roster_text):
        config = load_rig_config(write_config(tmp_path, COLLISION_CONFIG))
        roster = parse_roster(roster_text, Path("ROSTER.md"))
        return continue_collisions(roster, config, tmp_path)

    def test_no_warning_when_clis_differ(self, tmp_path):
        assert self.collisions(tmp_path, (
            "### Ana\n- **Rig:** solo\n- **Continue:** on\n\n"
            "### Bob\n- **Rig:** other\n"
        )) == []

    def test_no_warning_when_nobody_continues(self, tmp_path):
        assert self.collisions(tmp_path, (
            "### Ana\n- **Rig:** solo\n\n"
            "### Bob\n- **Rig:** solo\n"
        )) == []

    def test_warns_when_a_member_shares_the_cli(self, tmp_path):
        # Bob does not continue — he still writes the conversation Ana resumes.
        warnings = self.collisions(tmp_path, (
            "### Ana\n- **Rig:** solo\n- **Continue:** on\n\n"
            "### Bob\n- **Rig:** solo\n"
        ))
        assert len(warnings) == 1
        assert warnings[0].startswith("Ana: Continue: on")
        assert "Bob" in warnings[0] and "'claude'" in warnings[0]

    def test_warns_across_different_rigs_on_one_cli(self, tmp_path):
        warnings = self.collisions(tmp_path, (
            "### Ana\n- **Rig:** solo\n- **Continue:** on\n\n"
            "### Bob\n- **Rig:** twin\n"
        ))
        assert len(warnings) == 1 and "Bob" in warnings[0]

    def test_launcher_does_not_hide_a_shared_conversation(self, tmp_path):
        # One member reaches opencode through `ollama launch`, the other runs it
        # directly — same per-directory session store, so they still collide.
        warnings = self.collisions(tmp_path, (
            "### Ana\n- **Rig:** launched\n- **Continue:** on\n\n"
            "### Bob\n- **Rig:** cloud\n"
        ))
        assert len(warnings) == 1
        assert "Bob" in warnings[0] and "'opencode'" in warnings[0]
        assert "per directory" in warnings[0]

    def test_humans_and_broken_members_never_collide(self, tmp_path):
        assert self.collisions(tmp_path, (
            "### Ana\n- **Rig:** solo\n- **Continue:** on\n\n"
            "### Cid\n- **Human:** yes\n- **Address:** cid\n\n"
            "### Dot\n"
        )) == []

    def test_distinct_workdirs_on_one_cli_do_not_collide(self, tmp_path):
        # The conversation is keyed on (CLI, directory): a member with its own
        # Workdir: runs the shared CLI somewhere else entirely.
        assert self.collisions(tmp_path, (
            "### Ana\n- **Rig:** solo\n- **Continue:** on\n\n"
            "### Bob\n- **Rig:** solo\n- **Workdir:** agents/bob\n"
        )) == []

    def test_same_explicit_workdir_still_collides(self, tmp_path):
        warnings = self.collisions(tmp_path, (
            "### Ana\n- **Rig:** solo\n- **Continue:** on\n"
            "- **Workdir:** shared\n\n"
            "### Bob\n- **Rig:** solo\n- **Workdir:** shared\n"
        ))
        assert len(warnings) == 1 and "Bob" in warnings[0]

    def test_workdir_resolving_to_the_workplace_collides(self, tmp_path):
        warnings = self.collisions(tmp_path, (
            "### Ana\n- **Rig:** solo\n- **Continue:** on\n\n"
            "### Bob\n- **Rig:** solo\n- **Workdir:** .\n"
        ))
        assert len(warnings) == 1 and "Bob" in warnings[0]


class TestDefaultPayload:
    def test_init_payload_parses_with_both_rigs(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, default_config_payload()))
        assert set(config.rigs) == {"leader", "member"}
        for rig in config.rigs.values():
            assert rig.error is None
            assert any("{prompt}" in a for a in rig.pool()[0])


class TestHarnessPresets:
    def test_preset_names_match_a8s_kinds(self):
        assert preset_names() == [
            "agy", "claude", "codex", "copilot", "cursor", "ollama",
            "ollama-claude", "ollama-codex", "ollama-copilot", "ollama-opencode",
            "opencode",
        ]

    def test_every_preset_declares_a_known_text_tier(self):
        from rig import TEXT_TIERS

        tiers = {name: HARNESS_PRESETS[name]["text_tier"] for name in preset_names()}
        assert set(tiers.values()) <= set(TEXT_TIERS)
        assert tiers == {
            "agy": "big", "claude": "big", "codex": "big",
            "copilot": "moderate", "cursor": "moderate", "opencode": "moderate",
            "ollama": "small", "ollama-opencode": "small",
            "ollama-claude": "small", "ollama-codex": "small",
            "ollama-copilot": "small",
        }

    def test_text_tier_anchors(self):
        from rig import TEXT_TIERS

        assert TEXT_TIERS["big"]["history_max_bytes"] == 50_000
        assert TEXT_TIERS["moderate"]["history_max_bytes"] == 25_000
        assert TEXT_TIERS["small"] == {
            "history_max_bytes": 8192, "history_body_max": 2000,
            "prompt_body_max": 4000,
        }

    def test_no_preset_key_gets_small_defaults(self, tmp_path):
        config = load_rig_config(
            write_config(tmp_path, {"custom": {"invoke": ["my-cli", "{prompt}"]}})
        )
        rig = config.rigs["custom"]
        assert (rig.history_max_bytes, rig.history_body_max, rig.prompt_body_max) == (
            8192, 2000, 4000,
        )

    def test_unknown_preset_value_gets_small_defaults(self, tmp_path):
        config = load_rig_config(write_config(
            tmp_path,
            {"custom": {"invoke": ["x", "{prompt}"], "preset": "gemini"}},
        ))
        assert config.rigs["custom"].history_max_bytes == 8192

    def test_preset_tier_defaults_and_explicit_override(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {
            "big": {"invoke": ["codex", "{prompt}"], "preset": "codex"},
            "mid": {"invoke": ["copilot", "{prompt}"], "preset": "copilot"},
            "pinned": {
                "invoke": ["claude", "{prompt}"], "preset": "claude",
                "history_max_bytes": 999,
            },
        }))
        big, mid, pinned = (config.rigs[k] for k in ("big", "mid", "pinned"))
        assert (big.history_max_bytes, big.history_body_max, big.prompt_body_max) == (
            50_000, 12_000, 24_000,
        )
        assert (mid.history_max_bytes, mid.history_body_max, mid.prompt_body_max) == (
            25_000, 6_000, 12_000,
        )
        assert pinned.history_max_bytes == 999  # explicit wins over the tier
        assert pinned.history_body_max == 12_000  # untouched knobs stay tiered

    def test_every_preset_invoke_is_valid(self, tmp_path):
        for name in preset_names():
            config = load_rig_config(
                write_config(tmp_path, {name: {"invoke": HARNESS_PRESETS[name]["invoke"]}})
            )
            rig = config.rigs[name]
            assert rig.error is None
            assert "{prompt}" in format_preset_invoke(name)

    def test_add_preset_rig_writes_new_config(self, tmp_path):
        path = tmp_path / "rigs.json"
        rig_key = add_preset_rig(path, "worker", "claude")
        assert rig_key == "worker"
        config = load_rig_config(path)
        assert config.rigs["worker"].error is None
        assert config.rigs["worker"].argv("hi")[0] == "claude"

    def test_add_preset_rig_refuses_duplicate(self, tmp_path):
        path = write_config(tmp_path, {"worker": {"invoke": ["x", "{prompt}"]}})
        with pytest.raises(RigError, match="already exists"):
            add_preset_rig(path, "worker", "opencode")

    def test_add_preset_rig_force_replaces(self, tmp_path):
        path = write_config(tmp_path, {"worker": {"invoke": ["x", "{prompt}"]}})
        add_preset_rig(path, "worker", "opencode", force=True)
        config = load_rig_config(path)
        assert config.rigs["worker"].argv("hi")[0] == "opencode"

    def test_add_preset_rig_ollama_opencode_requires_model(self, tmp_path):
        path = tmp_path / "rigs.json"
        with pytest.raises(RigError, match="requires --model"):
            add_preset_rig(path, "worker", "ollama-opencode")

    def test_add_preset_rig_ollama_opencode_materializes_model(self, tmp_path):
        path = tmp_path / "rigs.json"
        rig_key = add_preset_rig(
            path, "worker", "ollama-opencode", model="qwen2.5-coder:7b"
        )
        assert rig_key == "worker"
        config = load_rig_config(path)
        argv = config.rigs["worker"].argv("hi")
        assert argv[4] == "qwen2.5-coder:7b"
        assert "{model}" not in argv

    def test_add_unknown_preset(self, tmp_path):
        path = tmp_path / "rigs.json"
        with pytest.raises(RigError, match="unknown preset"):
            add_preset_rig(path, "worker", "gemini")

    def test_swap_preset_rig_preserves_settings(self, tmp_path):
        path = write_config(tmp_path, {
            "worker": {
                "invoke": ["x", "{prompt}"],
                "concurrency": 3,
                "timeout_seconds": 120,
                "budget_max": 10,
                "budget_earn_per_hour": 2,
            },
        })
        rig_key = swap_preset_rig(path, "worker", "agy")
        assert rig_key == "worker"
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["worker"]["concurrency"] == 3
        assert raw["worker"]["timeout_seconds"] == 120
        assert raw["worker"]["budget_max"] == 10
        assert raw["worker"]["budget_earn_per_hour"] == 2
        assert "swap" in raw["worker"]["_notes"].lower()
        config = load_rig_config(path)
        assert config.rigs["worker"].argv("hi")[0] == "agy"
        assert config.rigs["worker"].concurrency == 3

    def test_add_records_preset_and_tier_defaults_apply(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "brain", "agy", model="sonnet")
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["brain"]["preset"] == "agy"
        rig = load_rig_config(path).rigs["brain"]
        assert rig.history_max_bytes == 50_000
        assert rig.history_body_max == 12_000
        assert rig.prompt_body_max == 24_000

    def test_swap_reresolves_tier_but_explicit_knob_wins(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "ollama", model="qwen3:0.6b")
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["worker"]["history_body_max"] = 1234  # operator's explicit value
        path.write_text(json.dumps(raw), encoding="utf-8")
        assert load_rig_config(path).rigs["worker"].history_max_bytes == 8192

        swap_preset_rig(path, "worker", "claude")
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["worker"]["preset"] == "claude"
        rig = load_rig_config(path).rigs["worker"]
        assert rig.history_max_bytes == 50_000  # re-resolved to the big tier
        assert rig.prompt_body_max == 24_000
        assert rig.history_body_max == 1234  # explicit value survives the swap

    def test_swap_preset_rig_missing_rig(self, tmp_path):
        path = write_config(tmp_path, {"other": {"invoke": ["x", "{prompt}"]}})
        with pytest.raises(RigError, match="no rig 'worker' to swap"):
            swap_preset_rig(path, "worker", "claude")

    def test_swap_preset_rig_missing_config(self, tmp_path):
        with pytest.raises(RigError, match="no rig"):
            swap_preset_rig(tmp_path / "rigs.json", "worker", "claude")

    def test_swap_unknown_preset(self, tmp_path):
        path = write_config(tmp_path, {"worker": {"invoke": ["x", "{prompt}"]}})
        with pytest.raises(RigError, match="unknown preset"):
            swap_preset_rig(path, "worker", "gemini")

    def test_swap_preset_rig_requires_model(self, tmp_path):
        path = write_config(tmp_path, {"worker": {"invoke": ["x", "{prompt}"]}})
        with pytest.raises(RigError, match="requires --model"):
            swap_preset_rig(path, "worker", "ollama-opencode")

    def test_swap_preset_rig_materializes_model(self, tmp_path):
        path = write_config(tmp_path, {
            "worker": {"invoke": ["x", "{prompt}"], "max_sends_per_turn": 4},
        })
        swap_preset_rig(path, "worker", "ollama-opencode", model="qwen2.5-coder:7b")
        config = load_rig_config(path)
        argv = config.rigs["worker"].argv("hi")
        assert argv[4] == "qwen2.5-coder:7b"
        assert "{model}" not in argv
        assert config.rigs["worker"].max_sends_per_turn == 4
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert "qwen2.5-coder:7b" in raw["worker"]["_notes"]

    def test_opencode_avoids_skip_permissions(self):
        opencode = " ".join(HARNESS_PRESETS["opencode"]["invoke"])
        assert "dangerously-skip-permissions" not in opencode
        assert "--auto" in opencode
        assert "-i" not in opencode

    def test_agy_preset_carries_skip_permissions(self):
        # agy 1.1.3+ auto-denies command tools in headless --print runs
        # (toolPermission=request-review can't prompt); accept-edits no longer
        # covers commands, so roster members that must run tell/git need the
        # skip. OS isolation is the security boundary, not this flag.
        agy = " ".join(HARNESS_PRESETS["agy"]["invoke"])
        assert "--dangerously-skip-permissions" in agy
        assert "--mode" in agy and "accept-edits" in agy
        assert "--print" in agy

    def test_build_preset_invoke_opencode(self):
        argv = build_preset_invoke("opencode")
        assert argv[0] == "opencode"
        assert "{prompt}" in argv

    def test_build_preset_invoke_ollama_opencode_requires_model(self):
        with pytest.raises(RigError, match="requires --model"):
            build_preset_invoke("ollama-opencode")
        argv = build_preset_invoke("ollama-opencode", model="qwen2.5-coder:7b")
        assert argv[:4] == ["ollama", "launch", "opencode", "--model"]
        assert argv[4] == "qwen2.5-coder:7b"
        assert argv[5:8] == ["--", "run", "--auto"]
        assert "{prompt}" in argv

    def test_build_preset_invoke_launch_wrapped_presets_require_model(self):
        for name in ("ollama-claude", "ollama-codex", "ollama-copilot"):
            with pytest.raises(RigError, match="requires --model"):
                build_preset_invoke(name)

    def test_build_preset_invoke_launch_wrapped_presets(self):
        parent_headless = {
            "ollama-claude": HARNESS_PRESETS["claude"]["invoke"][:-1],
            "ollama-codex": HARNESS_PRESETS["codex"]["invoke"][:-1],
            "ollama-copilot": HARNESS_PRESETS["copilot"]["invoke"][:-1],
        }
        for name, tail in parent_headless.items():
            argv = build_preset_invoke(name, model="qwen3.6:latest")
            parent = name.removeprefix("ollama-")
            assert argv[:7] == [
                "ollama", "launch", parent, "--model", "qwen3.6:latest", "-y", "--",
            ]
            assert argv[7:-1] == tail[1:]
            assert argv[-1] == "{prompt}"
            assert "-m" not in argv[7:]

    def test_build_preset_invoke_unknown(self):
        with pytest.raises(RigError, match="unknown preset"):
            build_preset_invoke("gemini")


AGY_MODEL_LIST = [
    "Gemini 3.5 Flash (Medium)",
    "Gemini 3.5 Flash (High)",
    "Gemini 3.5 Flash (Low)",
    "Gemini 3.1 Pro (Low)",
    "Gemini 3.1 Pro (High)",
    "Claude Sonnet 4.6 (Thinking)",
    "Claude Opus 4.6 (Thinking)",
    "GPT-OSS 120B (Medium)",
]


class TestModelSplice:
    """--model splices at the right position for each preset (see the ruling on
    issue #186: static presets splice now, agy keeps a placeholder for live
    resolution)."""

    def test_optional_when_absent_returns_base(self):
        for preset in ("claude", "codex", "opencode", "agy"):
            assert build_preset_invoke(preset) == HARNESS_PRESETS[preset]["invoke"]

    def test_cursor_pins_auto_when_no_model_is_given(self):
        # `agent` reuses the last --model used on the machine when the flag is
        # omitted, so the bare preset must still name one (#275).
        argv = build_preset_invoke("cursor")
        assert argv[:3] == ["agent", "--model", "auto"]
        assert argv[-1] == "{prompt}"
        assert format_preset_invoke("cursor").startswith("agent --model auto")

    def test_claude_splices_after_executable(self):
        argv = build_preset_invoke("claude", model="sonnet")
        assert argv[:3] == ["claude", "--model", "sonnet"]
        assert argv[-2:] == ["-p", "{prompt}"]

    def test_codex_splices_after_exec(self):
        argv = build_preset_invoke("codex", model="gpt-5.6-sol")
        assert argv[:4] == ["codex", "exec", "-m", "gpt-5.6-sol"]
        assert argv[-1] == "{prompt}"

    def test_cursor_splices_after_executable(self):
        argv = build_preset_invoke("cursor", model="sonnet-4-thinking")
        assert argv[:3] == ["agent", "--model", "sonnet-4-thinking"]
        assert "auto" not in argv

    def test_cursor_add_without_model_records_the_auto_pin(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "solo", "cursor")
        assert load_rig_config(path).rigs["solo"].invoke[:3] == [
            "agent", "--model", "auto",
        ]
        add_preset_rig(path, "pinned", "cursor", model="sonnet-4-thinking")
        assert load_rig_config(path).rigs["pinned"].invoke[:3] == [
            "agent", "--model", "sonnet-4-thinking",
        ]

    def test_opencode_splices_after_run(self):
        argv = build_preset_invoke("opencode", model="anthropic/claude-sonnet-4-5")
        assert argv[:4] == ["opencode", "run", "-m", "anthropic/claude-sonnet-4-5"]
        assert "--auto" in argv

    def test_agy_keeps_placeholder_for_live_resolution(self):
        argv = build_preset_invoke("agy", model="sonnet")
        assert argv[:3] == ["agy", "--model", "{model}"]
        assert "sonnet" not in argv

    def test_ollama_requires_model_and_substitutes(self):
        with pytest.raises(RigError, match="requires --model"):
            build_preset_invoke("ollama")
        argv = build_preset_invoke("ollama", model="qwen2.5-coder:7b")
        assert argv == ["ollama", "run", "qwen2.5-coder:7b", "{prompt}"]

    def test_preset_without_model_support_refuses_model(self):
        with pytest.raises(RigError, match="does not support --model"):
            build_preset_invoke("copilot", model="opus")

    def test_agy_add_persists_model_and_resolver(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "brain", "agy", model="sonnet")
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["brain"]["model"] == "sonnet"
        assert raw["brain"]["model_resolver"] == "agy-live"
        config = load_rig_config(path)
        assert config.rigs["brain"].model == "sonnet"
        assert config.rigs["brain"].model_resolver == "agy-live"

    def test_static_add_does_not_persist_resolver(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "coder", "claude", model="opus")
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert "model_resolver" not in raw["coder"]
        assert raw["coder"]["invoke"][:3] == ["claude", "--model", "opus"]

    def test_swap_off_agy_drops_stale_resolver(self, tmp_path):
        path = write_config(tmp_path, {"worker": {"invoke": ["x", "{prompt}"]}})
        swap_preset_rig(path, "worker", "agy", model="sonnet")
        assert load_rig_config(path).rigs["worker"].model_resolver == "agy-live"
        swap_preset_rig(path, "worker", "claude", model="opus")
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert "model_resolver" not in raw["worker"]
        assert "model" not in raw["worker"]


class TestFuzzyMatchModel:
    @pytest.mark.parametrize("query,expected", [
        ("sonnet", "Claude Sonnet 4.6 (Thinking)"),
        ("opus", "Claude Opus 4.6 (Thinking)"),
        ("gpt-oss", "GPT-OSS 120B (Medium)"),
        ("gpt-oss-120b", "GPT-OSS 120B (Medium)"),
        ("claude-sonnet", "Claude Sonnet 4.6 (Thinking)"),
        ("gemini-3.5-flash", "Gemini 3.5 Flash (High)"),
        ("Claude Sonnet 4.6 (Thinking)", "Claude Sonnet 4.6 (Thinking)"),
    ])
    def test_resolves(self, query, expected):
        assert fuzzy_match_model(query, AGY_MODEL_LIST) == expected

    def test_dashes_and_spaces_interchangeable(self):
        assert fuzzy_match_model("gemini 3.5 flash", AGY_MODEL_LIST) == fuzzy_match_model(
            "gemini-3.5-flash", AGY_MODEL_LIST
        )

    def test_tiebreak_prefers_highest_effort(self):
        # flash hits Low/Medium/High; the effort tie-break picks High.
        assert fuzzy_match_model("flash", AGY_MODEL_LIST) == "Gemini 3.5 Flash (High)"

    def test_ambiguous_family_tiebreak_is_deterministic(self):
        # gemini hits every Gemini line; extra-token count ties, effort favors
        # the High variants, alphabetical breaks Pro vs Flash -> Pro.
        assert fuzzy_match_model("gemini", AGY_MODEL_LIST) == "Gemini 3.1 Pro (High)"
        assert fuzzy_match_model("pro", AGY_MODEL_LIST) == "Gemini 3.1 Pro (High)"

    def test_garbage_errors_loudly_with_listing(self):
        with pytest.raises(RigError) as exc:
            fuzzy_match_model("banana", AGY_MODEL_LIST)
        msg = str(exc.value)
        assert "matched no agy model" in msg
        assert "Claude Sonnet 4.6 (Thinking)" in msg

    def test_resolve_agy_model_uses_injected_names(self):
        assert resolve_agy_model("opus", names=AGY_MODEL_LIST) == "Claude Opus 4.6 (Thinking)"


class TestRemoveRig:
    def test_remove_deletes_entry(self, tmp_path):
        path = write_config(tmp_path, {
            "worker": {"invoke": ["x", "{prompt}"]},
            "spare": {"invoke": ["y", "{prompt}"]},
        })
        assert remove_rig(path, "worker") == "worker"
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert "worker" not in raw
        assert "spare" in raw

    def test_remove_unknown_errors(self, tmp_path):
        path = write_config(tmp_path, {"worker": {"invoke": ["x", "{prompt}"]}})
        with pytest.raises(RigError, match="no rig 'ghost' to remove"):
            remove_rig(path, "ghost")

    def test_remove_missing_config_errors(self, tmp_path):
        with pytest.raises(RigError, match="no rig"):
            remove_rig(tmp_path / "rigs.json", "worker")


class TestRemoveCLI:
    def test_remove_via_cli(self, tmp_path):
        path = write_config(tmp_path, {
            "a": {"invoke": ["x", "{prompt}"]},
            "b": {"invoke": ["y", "{prompt}"]},
        })
        assert r4t_main(["rig", "remove", "a", "--rig-config", str(path)]) == 0
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert "a" not in raw and "b" in raw

    def test_rm_alias(self, tmp_path):
        path = write_config(tmp_path, {"a": {"invoke": ["x", "{prompt}"]}})
        assert r4t_main(["rig", "rm", "a", "--rig-config", str(path)]) == 0
        assert "a" not in json.loads(path.read_text(encoding="utf-8"))

    def test_remove_multiple(self, tmp_path):
        path = write_config(tmp_path, {
            "a": {"invoke": ["x", "{prompt}"]},
            "b": {"invoke": ["y", "{prompt}"]},
            "c": {"invoke": ["z", "{prompt}"]},
        })
        assert r4t_main(["rig", "remove", "a", "b", "--rig-config", str(path)]) == 0
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert "a" not in raw and "b" not in raw and "c" in raw

    def test_remove_unknown_returns_1(self, tmp_path):
        path = write_config(tmp_path, {"a": {"invoke": ["x", "{prompt}"]}})
        assert r4t_main(["rig", "remove", "ghost", "--rig-config", str(path)]) == 1

    def test_remove_refuses_pinned_rig(self, tmp_path):
        path = write_config(tmp_path, {
            "a": {"invoke": ["x", "{prompt}"]},
            "pins": {"phil": "a"},
        })
        assert r4t_main(["rig", "remove", "a", "--rig-config", str(path)]) == 1
        assert "a" in json.loads(path.read_text(encoding="utf-8"))

    def test_remove_force_ignores_pin(self, tmp_path):
        path = write_config(tmp_path, {
            "a": {"invoke": ["x", "{prompt}"]},
            "pins": {"phil": "a"},
        })
        assert r4t_main(
            ["rig", "remove", "a", "--force", "--rig-config", str(path)]
        ) == 0
        assert "a" not in json.loads(path.read_text(encoding="utf-8"))


class TestRigSettingsCore:
    def test_get_all_keys_covered(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        keys = [s.key for s in rig_settings(path, "worker")]
        assert keys == list(CONFIGURABLE_RIG_KEYS)

    def test_concurrency_default_and_explicit_source(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        s = rig_setting(path, "worker", "concurrency")
        assert (s.value, s.explicit, s.source) == (DEFAULT_CONCURRENCY, False, "built-in default")
        set_rig_value(path, "worker", "concurrency", "4")
        s = rig_setting(path, "worker", "concurrency")
        assert (s.value, s.explicit, s.source) == (4, True, "explicit")

    def test_text_knob_inherits_from_preset_tier(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        s = rig_setting(path, "worker", "history_max_bytes")
        assert s.value == 25_000
        assert s.explicit is False
        assert s.source == "from preset opencode"

    def test_text_knob_no_preset_is_built_in(self, tmp_path):
        path = write_config(tmp_path, {"custom": {"invoke": ["x", "{prompt}"]}})
        s = rig_setting(path, "custom", "history_max_bytes")
        assert (s.value, s.source, s.explicit) == (8192, "built-in default", False)

    def test_rig_budget_unset_by_default(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        s = rig_setting(path, "worker", "rig_budget_max")
        assert s.value is None
        assert s.explicit is False
        assert s.display() == "unset"

    def test_set_get_unset_round_trip(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        set_rig_value(path, "worker", "history_max_bytes", "9999")
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["worker"]["history_max_bytes"] == 9999
        assert rig_setting(path, "worker", "history_max_bytes").value == 9999
        assert unset_rig_value(path, "worker", "history_max_bytes") is True
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert "history_max_bytes" not in raw["worker"]
        # falls back to the preset tier, not materialized
        s = rig_setting(path, "worker", "history_max_bytes")
        assert s.value == 25_000 and s.explicit is False

    def test_enter_keeps_inherited_does_not_materialize(self, tmp_path):
        # The configure loop skips keys the operator leaves blank, so nothing
        # inherited is written — swap re-resolution depends on this.
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        before = json.loads(path.read_text(encoding="utf-8"))["worker"]
        set_rig_value(path, "worker", "concurrency", "2")
        raw = json.loads(path.read_text(encoding="utf-8"))["worker"]
        assert raw["concurrency"] == 2
        assert "history_max_bytes" not in raw
        assert "rig_budget_max" not in raw
        assert before.get("preset") == raw.get("preset")

    def test_unset_unset_key_is_noop(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        assert unset_rig_value(path, "worker", "concurrency") is False

    def test_unknown_key_errors_loudly(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        for fn in (
            lambda: rig_setting(path, "worker", "bogus"),
            lambda: set_rig_value(path, "worker", "bogus", "1"),
            lambda: unset_rig_value(path, "worker", "bogus"),
        ):
            with pytest.raises(RigError, match="unknown rig setting"):
                fn()

    def test_numeric_validation(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        with pytest.raises(RigError, match="must be a number"):
            set_rig_value(path, "worker", "concurrency", "abc")
        with pytest.raises(RigError, match="whole number"):
            set_rig_value(path, "worker", "concurrency", "2.5")
        with pytest.raises(RigError, match="positive"):
            set_rig_value(path, "worker", "concurrency", "0")

    def test_float_key_accepts_decimals(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        set_rig_value(path, "worker", "rig_budget_max", "20")
        set_rig_value(path, "worker", "rig_budget_earn_per_hour", "2.5")
        raw = json.loads(path.read_text(encoding="utf-8"))["worker"]
        assert raw["rig_budget_max"] == 20
        assert raw["rig_budget_earn_per_hour"] == 2.5

    def test_set_missing_rig_errors(self, tmp_path):
        path = write_config(tmp_path, {"other": {"invoke": ["x", "{prompt}"]}})
        with pytest.raises(RigError, match="no rig 'worker'"):
            set_rig_value(path, "worker", "concurrency", "2")


class TestEchoSetting:
    def test_default_off(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "ollama", model="tiny")
        rig = load_rig_config(path).rigs["worker"]
        assert rig.echo is False
        assert rig.echo_max_chars == DEFAULT_ECHO_MAX_CHARS == 1500
        s = rig_setting(path, "worker", "echo")
        assert (s.value, s.explicit, s.source) == (False, False, "built-in default")
        assert s.display() == "false"

    def test_set_get_unset_round_trip(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "ollama", model="tiny")
        set_rig_value(path, "worker", "echo", "true")
        assert json.loads(path.read_text(encoding="utf-8"))["worker"]["echo"] is True
        assert load_rig_config(path).rigs["worker"].echo is True
        s = rig_setting(path, "worker", "echo")
        assert (s.value, s.explicit, s.display()) == (True, True, "true")
        set_rig_value(path, "worker", "echo", "false")
        assert load_rig_config(path).rigs["worker"].echo is False
        assert unset_rig_value(path, "worker", "echo") is True
        assert "echo" not in json.loads(path.read_text(encoding="utf-8"))["worker"]

    def test_set_rejects_non_boolean(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "ollama", model="tiny")
        with pytest.raises(RigError, match="must be true or false"):
            set_rig_value(path, "worker", "echo", "yes")

    def test_parse_rejects_non_boolean_json(self, tmp_path):
        path = write_config(
            tmp_path, {"worker": {"invoke": ["x", "{prompt}"], "echo": "true"}}
        )
        rig = load_rig_config(path).rigs["worker"]
        assert "echo: expected true or false" in rig.error

    def test_max_chars_set_and_validation(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "ollama", model="tiny")
        set_rig_value(path, "worker", "echo_max_chars", "400")
        assert load_rig_config(path).rigs["worker"].echo_max_chars == 400
        with pytest.raises(RigError, match="positive"):
            set_rig_value(path, "worker", "echo_max_chars", "0")

    def test_set_via_cli(self, tmp_path, capsys):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "ollama", model="tiny")
        assert r4t_main(
            ["rig", "set", "worker", "echo", "true", "--rig-config", str(path)]
        ) == 0
        assert "set worker echo = true" in capsys.readouterr().out
        assert r4t_main(
            ["rig", "get", "worker", "echo", "--rig-config", str(path)]
        ) == 0
        assert capsys.readouterr().out.strip() == "true"


class TestRigModelSetting:
    def test_set_model_agy_keeps_live_resolver(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "brain", "agy", model="sonnet")
        set_rig_value(path, "brain", "model", "opus")
        raw = json.loads(path.read_text(encoding="utf-8"))["brain"]
        assert raw["model"] == "opus"
        assert raw["model_resolver"] == "agy-live"
        assert raw["invoke"][:3] == ["agy", "--model", "{model}"]
        assert rig_setting(path, "brain", "model").value == "opus"

    def test_set_model_static_bakes_into_invoke(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "coder", "claude")
        set_rig_value(path, "coder", "model", "opus")
        raw = json.loads(path.read_text(encoding="utf-8"))["coder"]
        assert raw["invoke"][:3] == ["claude", "--model", "opus"]
        assert "model" not in raw

    def test_set_model_without_preset_errors(self, tmp_path):
        path = write_config(tmp_path, {"raw": {"invoke": ["x", "{prompt}"]}})
        with pytest.raises(RigError, match="no recorded preset"):
            set_rig_value(path, "raw", "model", "opus")

    def test_set_model_unsupported_preset_errors(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "cop", "copilot")
        with pytest.raises(RigError, match="does not support --model"):
            set_rig_value(path, "cop", "model", "opus")

    def test_unset_model_static_reverts_to_base(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "coder", "claude", model="opus")
        assert unset_rig_value(path, "coder", "model") is True
        raw = json.loads(path.read_text(encoding="utf-8"))["coder"]
        assert raw["invoke"] == HARNESS_PRESETS["claude"]["invoke"]

    def test_unset_model_agy_drops_resolver(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "brain", "agy", model="sonnet")
        assert unset_rig_value(path, "brain", "model") is True
        raw = json.loads(path.read_text(encoding="utf-8"))["brain"]
        assert "model" not in raw and "model_resolver" not in raw
        assert raw["invoke"] == HARNESS_PRESETS["agy"]["invoke"]


class TestRigConfigureCLI:
    def test_set_get_unset_via_cli(self, tmp_path, capsys):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        assert r4t_main(
            ["rig", "set", "worker", "concurrency", "3", "--rig-config", str(path)]
        ) == 0
        capsys.readouterr()
        assert r4t_main(
            ["rig", "get", "worker", "concurrency", "--rig-config", str(path)]
        ) == 0
        out = capsys.readouterr()
        assert out.out.strip() == "3"
        assert "(explicit)" in out.err
        assert r4t_main(
            ["rig", "unset", "worker", "concurrency", "--rig-config", str(path)]
        ) == 0
        assert "concurrency" not in json.loads(path.read_text(encoding="utf-8"))["worker"]

    def test_get_bare_lists_all(self, tmp_path, capsys):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        assert r4t_main(["rig", "get", "worker", "--rig-config", str(path)]) == 0
        out = capsys.readouterr().out
        for key in CONFIGURABLE_RIG_KEYS:
            assert key in out
        assert "from preset opencode" in out

    def test_set_unknown_key_returns_1(self, tmp_path, capsys):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        assert r4t_main(
            ["rig", "set", "worker", "bogus", "1", "--rig-config", str(path)]
        ) == 1
        assert "unknown rig setting" in capsys.readouterr().err

    def test_set_bad_number_returns_1(self, tmp_path, capsys):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        assert r4t_main(
            ["rig", "set", "worker", "concurrency", "abc", "--rig-config", str(path)]
        ) == 1
        assert "must be a number" in capsys.readouterr().err

    def test_unset_multiple_keys(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        set_rig_value(path, "worker", "concurrency", "2")
        set_rig_value(path, "worker", "history_max_bytes", "1000")
        assert r4t_main(
            ["rig", "unset", "worker", "concurrency", "history_max_bytes",
             "--rig-config", str(path)]
        ) == 0
        raw = json.loads(path.read_text(encoding="utf-8"))["worker"]
        assert "concurrency" not in raw and "history_max_bytes" not in raw

    def test_configure_piped_sets_one_keeps_rest(self, tmp_path, capsys, monkeypatch):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        answers = iter(["5", "", ""])

        def piped(prompt=""):
            try:
                return next(answers)
            except StopIteration:
                raise EOFError

        monkeypatch.setattr("builtins.input", piped)
        assert r4t_main(["rig", "configure", "worker", "--rig-config", str(path)]) == 0
        raw = json.loads(path.read_text(encoding="utf-8"))["worker"]
        assert raw["concurrency"] == 5
        assert "rig_budget_max" not in raw
        assert "history_max_bytes" not in raw

    def test_configure_piped_eof_keeps_rest(self, tmp_path, monkeypatch):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        def eof(prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)
        assert r4t_main(["rig", "configure", "worker", "--rig-config", str(path)]) == 0
        raw = json.loads(path.read_text(encoding="utf-8"))["worker"]
        assert "concurrency" not in raw

    def test_configure_piped_invalid_errors_loudly(self, tmp_path, capsys, monkeypatch):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        answers = iter(["notanumber"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
        assert r4t_main(["rig", "configure", "worker", "--rig-config", str(path)]) == 1
        assert "must be a number" in capsys.readouterr().err

    def test_configure_missing_rig_returns_1(self, tmp_path, capsys):
        path = write_config(tmp_path, {"other": {"invoke": ["x", "{prompt}"]}})
        assert r4t_main(["rig", "configure", "ghost", "--rig-config", str(path)]) == 1
        assert "no rig 'ghost'" in capsys.readouterr().err


def _mcp_rig(tmp_path, preset, model=None):
    path = tmp_path / "rigs.json"
    add_preset_rig(path, "worker", preset, model=model, force=True)
    set_rig_value(path, "worker", "mcp", "on")
    return load_rig_config(path).rigs["worker"]


class TestMcpSetting:
    @pytest.mark.parametrize("preset", ["claude", "codex", "copilot", "opencode"])
    def test_unset_defaults_on_where_the_idiom_is_invisible_to_the_repo(
        self, tmp_path, preset
    ):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", preset)
        rig = load_rig_config(path).rigs["worker"]
        assert rig.mcp is None
        assert rig.mcp_on is True
        s = rig_setting(path, "worker", "mcp")
        assert (s.value, s.explicit, s.source, s.display()) == (
            True, False, f"from preset {preset}", "true"
        )

    def test_unset_defaults_off_for_cursor_which_would_write_into_the_repo(
        self, tmp_path
    ):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "cursor")
        rig = load_rig_config(path).rigs["worker"]
        assert (rig.mcp, rig.mcp_on) == (None, False)
        s = rig_setting(path, "worker", "mcp")
        assert (s.value, s.explicit, s.source) == (False, False, "from preset cursor")

    @pytest.mark.parametrize("preset,model", [("agy", None), ("ollama", "tiny")])
    def test_unset_resolves_off_silently_without_an_idiom(self, tmp_path, preset, model):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", preset, model=model)
        rig = load_rig_config(path).rigs["worker"]
        assert (rig.mcp, rig.mcp_on, rig.error) == (None, False, None)

    def test_presetless_rig_resolves_off(self, tmp_path):
        path = write_config(tmp_path, {"worker": {"invoke": ["x", "{prompt}"]}})
        rig = load_rig_config(path).rigs["worker"]
        assert (rig.mcp, rig.mcp_on) == (None, False)
        s = rig_setting(path, "worker", "mcp")
        assert (s.value, s.explicit, s.source) == (False, False, "built-in default")

    def test_explicit_off_beats_a_default_on_preset(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "claude")
        set_rig_value(path, "worker", "mcp", "off")
        rig = load_rig_config(path).rigs["worker"]
        assert (rig.mcp, rig.mcp_on) == (False, False)
        s = rig_setting(path, "worker", "mcp")
        assert (s.value, s.explicit, s.source) == (False, True, "explicit")
        assert unset_rig_value(path, "worker", "mcp") is True
        assert load_rig_config(path).rigs["worker"].mcp_on is True

    def test_a_fresh_rig_carries_no_mcp_key(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "claude")
        assert "mcp" not in json.loads(path.read_text(encoding="utf-8"))["worker"]
        set_rig_value(path, "worker", "mcp", "off")
        assert json.loads(path.read_text(encoding="utf-8"))["worker"]["mcp"] is False
        unset_rig_value(path, "worker", "mcp")
        assert "mcp" not in json.loads(path.read_text(encoding="utf-8"))["worker"]

    def test_set_get_unset_round_trip(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        set_rig_value(path, "worker", "mcp", "on")
        assert json.loads(path.read_text(encoding="utf-8"))["worker"]["mcp"] is True
        assert load_rig_config(path).rigs["worker"].mcp is True
        set_rig_value(path, "worker", "mcp", "off")
        assert load_rig_config(path).rigs["worker"].mcp is False
        assert unset_rig_value(path, "worker", "mcp") is True
        assert "mcp" not in json.loads(path.read_text(encoding="utf-8"))["worker"]

    def test_true_and_false_still_accepted(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        set_rig_value(path, "worker", "mcp", "true")
        assert load_rig_config(path).rigs["worker"].mcp is True
        set_rig_value(path, "worker", "mcp", "false")
        assert load_rig_config(path).rigs["worker"].mcp is False

    def test_set_rejects_non_boolean(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        with pytest.raises(RigError, match="must be true or false"):
            set_rig_value(path, "worker", "mcp", "maybe")

    def test_mcp_presets_exclude_agy_and_bare_ollama(self):
        names = mcp_presets()
        assert "agy" not in names and "ollama" not in names
        assert {"claude", "codex", "copilot", "cursor", "opencode"} <= set(names)
        for name in names:
            assert HARNESS_PRESETS[name].get("mcp")

    def test_agy_refuses_the_knob_with_a_try_hint(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "agy")
        with pytest.raises(RigError) as excinfo:
            set_rig_value(path, "worker", "mcp", "on")
        message = str(excinfo.value)
        assert message.startswith("leave mcp off for rig 'worker'")
        assert "~/.gemini" in message
        assert "(try: r4t rig swap worker" in message
        assert "mcp" not in json.loads(path.read_text(encoding="utf-8"))["worker"]

    def test_bare_ollama_refuses_the_knob(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "ollama", model="tiny")
        with pytest.raises(RigError, match="no tool use at all"):
            set_rig_value(path, "worker", "mcp", "on")

    def test_presetless_rig_refuses_the_knob(self, tmp_path):
        path = write_config(tmp_path, {"worker": {"invoke": ["x", "{prompt}"]}})
        with pytest.raises(RigError, match="records no preset"):
            set_rig_value(path, "worker", "mcp", "on")

    def test_turning_it_off_is_always_allowed(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "agy")
        set_rig_value(path, "worker", "mcp", "off")
        assert load_rig_config(path).rigs["worker"].mcp is False

    def test_hand_edited_true_on_agy_fails_the_rig_closed(self, tmp_path):
        path = write_config(
            tmp_path,
            {"worker": {"preset": "agy", "invoke": ["agy", "{prompt}"], "mcp": True}},
        )
        rig = load_rig_config(path).rigs["worker"]
        assert rig.error and "~/.gemini" in rig.error
        assert (rig.mcp, rig.mcp_on) == (None, False)
        assert load_rig_config(path).rig_for(member(rig="worker"))[0] is None

    def test_parse_rejects_non_boolean_json(self, tmp_path):
        path = write_config(
            tmp_path, {"worker": {"invoke": ["x", "{prompt}"], "mcp": "true"}}
        )
        assert "mcp: expected true or false" in load_rig_config(path).rigs["worker"].error

    def test_set_via_cli(self, tmp_path, capsys):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "opencode")
        assert r4t_main(
            ["rig", "set", "worker", "mcp", "on", "--rig-config", str(path)]
        ) == 0
        assert "set worker mcp = true" in capsys.readouterr().out

    def test_agy_via_cli_returns_1_action_first(self, tmp_path, capsys):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "agy")
        assert r4t_main(
            ["rig", "set", "worker", "mcp", "on", "--rig-config", str(path)]
        ) == 1
        err = capsys.readouterr().err
        assert err.startswith("leave mcp off")
        assert "(try:" in err


class TestMcpInjection:
    def _turn(self, tmp_path):
        staging = tmp_path / "state" / "worker" / "staging"
        staging.mkdir(parents=True)
        cwd = tmp_path / "repo"
        cwd.mkdir()
        return {"TELL_OUTBOX_DIR": str(staging), "HOME": str(tmp_path)}, cwd

    def test_claude_gets_mcp_config_and_an_allowlisted_tool(self, tmp_path):
        rig = _mcp_rig(tmp_path, "claude")
        env, cwd = self._turn(tmp_path)
        argv = apply_mcp(rig, rig.argv("PROMPT"), env, cwd).argv

        assert argv[1] == "--mcp-config"
        server = json.loads(argv[2])["mcpServers"]["a8s"]
        assert server["args"][-2:] == ["mcp", "serve"]
        assert server["env"]["TELL_OUTBOX_DIR"] == env["TELL_OUTBOX_DIR"]
        allowed = argv[argv.index("--allowedTools") + 1]
        assert "mcp__a8s__tell" in allowed
        assert "Bash(tell:*)" in allowed

    def test_claude_ollama_splices_after_the_launcher_separator(self, tmp_path):
        rig = _mcp_rig(tmp_path, "ollama-claude", model="qwen3.6")
        env, cwd = self._turn(tmp_path)
        argv = apply_mcp(rig, rig.argv("PROMPT"), env, cwd).argv

        assert argv[argv.index("--") + 1] == "--mcp-config"
        assert "mcp__a8s__tell" in argv[argv.index("--allowedTools") + 1]

    def test_codex_gets_a_toml_override_with_cwd_pinned(self, tmp_path):
        rig = _mcp_rig(tmp_path, "codex")
        env, cwd = self._turn(tmp_path)
        argv = apply_mcp(rig, rig.argv("PROMPT"), env, cwd).argv

        assert argv[1] == "-c"
        override = argv[2]
        assert override.startswith("mcp_servers.a8s={")
        assert '"mcp", "serve"' in override
        assert f'cwd = "{cwd}"' in override
        assert f'TELL_OUTBOX_DIR = "{env["TELL_OUTBOX_DIR"]}"' in override

    def test_codex_ollama_splices_after_the_launcher_separator(self, tmp_path):
        rig = _mcp_rig(tmp_path, "ollama-codex", model="qwen3.6")
        env, cwd = self._turn(tmp_path)
        argv = apply_mcp(rig, rig.argv("PROMPT"), env, cwd).argv
        assert argv[argv.index("--") + 1] == "-c"

    def test_copilot_gets_an_additional_config(self, tmp_path):
        rig = _mcp_rig(tmp_path, "copilot")
        env, cwd = self._turn(tmp_path)
        argv = apply_mcp(rig, rig.argv("PROMPT"), env, cwd).argv

        assert argv[1] == "--additional-mcp-config"
        assert json.loads(argv[2])["mcpServers"]["a8s"]["command"]

    def test_copilot_ollama_splices_after_the_launcher_separator(self, tmp_path):
        rig = _mcp_rig(tmp_path, "ollama-copilot", model="qwen3.6")
        env, cwd = self._turn(tmp_path)
        argv = apply_mcp(rig, rig.argv("PROMPT"), env, cwd).argv
        assert argv[argv.index("--") + 1] == "--additional-mcp-config"

    def test_opencode_rides_a_config_file_never_config_content(self, tmp_path):
        rig = _mcp_rig(tmp_path, "opencode")
        env, cwd = self._turn(tmp_path)
        argv = apply_mcp(rig, rig.argv("PROMPT"), env, cwd).argv

        assert argv == rig.argv("PROMPT")
        assert "OPENCODE_CONFIG_CONTENT" not in env
        config = Path(env["OPENCODE_CONFIG"])
        # A dir of its own beside the staging outbox: nothing else lives there,
        # so a container can mount it without exposing the member's transcripts,
        # and a `.json` here is never mistaken for a staged envelope.
        assert config.parent == Path(env["TELL_OUTBOX_DIR"]).parent / "mcp"
        assert cwd not in config.parents
        server = json.loads(config.read_text(encoding="utf-8"))["mcp"]["a8s"]
        assert server["type"] == "local"
        assert server["enabled"] is True
        assert server["command"][-2:] == ["mcp", "serve"]
        assert server["environment"]["TELL_OUTBOX_DIR"] == env["TELL_OUTBOX_DIR"]

    def test_opencode_ollama_uses_the_same_file_idiom(self, tmp_path):
        rig = _mcp_rig(tmp_path, "ollama-opencode", model="qwen3.6")
        env, cwd = self._turn(tmp_path)
        argv = apply_mcp(rig, rig.argv("PROMPT"), env, cwd).argv
        assert argv == rig.argv("PROMPT")
        assert Path(env["OPENCODE_CONFIG"]).is_file()
        assert "OPENCODE_CONFIG_CONTENT" not in env

    def test_cursor_drops_a_file_and_keeps_other_servers(self, tmp_path):
        rig = _mcp_rig(tmp_path, "cursor")
        env, cwd = self._turn(tmp_path)
        existing = cwd / ".cursor" / "mcp.json"
        existing.parent.mkdir()
        existing.write_text(
            json.dumps({"mcpServers": {"theirs": {"command": "x"}}}), encoding="utf-8"
        )

        argv = apply_mcp(rig, rig.argv("PROMPT"), env, cwd).argv
        assert argv == rig.argv("PROMPT")
        servers = json.loads(existing.read_text(encoding="utf-8"))["mcpServers"]
        assert set(servers) == {"theirs", "a8s"}
        assert servers["a8s"]["env"]["TELL_OUTBOX_DIR"] == env["TELL_OUTBOX_DIR"]

    def test_cursor_write_is_idempotent(self, tmp_path):
        rig = _mcp_rig(tmp_path, "cursor")
        env, cwd = self._turn(tmp_path)
        apply_mcp(rig, rig.argv("PROMPT"), env, cwd)
        path = cwd / ".cursor" / "mcp.json"
        stamp = path.stat().st_mtime_ns
        apply_mcp(rig, rig.argv("PROMPT"), env, cwd)
        assert path.stat().st_mtime_ns == stamp

    def test_a8s_mcp_log_is_pinned_when_set(self, tmp_path):
        rig = _mcp_rig(tmp_path, "opencode")
        env, cwd = self._turn(tmp_path)
        env["A8S_MCP_LOG"] = str(tmp_path / "calls.jsonl")
        apply_mcp(rig, rig.argv("PROMPT"), env, cwd)
        config = json.loads(Path(env["OPENCODE_CONFIG"]).read_text(encoding="utf-8"))
        assert config["mcp"]["a8s"]["environment"]["A8S_MCP_LOG"] == env["A8S_MCP_LOG"]


class TestRigEnvMap:
    """The `env` map: static harness knobs on the rig (issue #284). Frugal by
    doctrine, and never a way to reach r4t's own turn variables."""

    def test_absent_by_default_and_keyless_in_the_file(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "claude")
        assert load_rig_config(path).rigs["worker"].env == {}
        assert "env" not in json.loads(path.read_text(encoding="utf-8"))["worker"]

    def test_set_get_unset_round_trip(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "claude")
        s = set_rig_value(path, "worker", "env.ENABLE_PROMPT_CACHING_1H", "1")
        assert (s.key, s.value, s.explicit, s.display()) == (
            "env.ENABLE_PROMPT_CACHING_1H", "1", True, "1"
        )
        raw = json.loads(path.read_text(encoding="utf-8"))["worker"]
        assert raw["env"] == {"ENABLE_PROMPT_CACHING_1H": "1"}
        assert load_rig_config(path).rigs["worker"].env == {
            "ENABLE_PROMPT_CACHING_1H": "1"
        }
        got = rig_setting(path, "worker", "env.ENABLE_PROMPT_CACHING_1H")
        assert (got.value, got.source) == ("1", "explicit")
        assert unset_rig_value(path, "worker", "env.ENABLE_PROMPT_CACHING_1H") is True
        # An empty map is no map — nothing is inherited for it to shadow.
        assert "env" not in json.loads(path.read_text(encoding="utf-8"))["worker"]

    def test_unset_of_an_unset_name_is_a_noop(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "claude")
        assert unset_rig_value(path, "worker", "env.NOPE") is False
        set_rig_value(path, "worker", "env.KEEP", "1")
        assert unset_rig_value(path, "worker", "env.NOPE") is False
        assert json.loads(path.read_text(encoding="utf-8"))["worker"]["env"] == {
            "KEEP": "1"
        }

    def test_second_entry_joins_rather_than_replaces(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "claude")
        set_rig_value(path, "worker", "env.ENABLE_PROMPT_CACHING_1H", "1")
        set_rig_value(path, "worker", "env.MAX_THINKING_TOKENS", "8000")
        assert load_rig_config(path).rigs["worker"].env == {
            "ENABLE_PROMPT_CACHING_1H": "1",
            "MAX_THINKING_TOKENS": "8000",
        }

    def test_names_keep_their_case_the_prefix_does_not(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "claude")
        set_rig_value(path, "worker", "ENV.MixedCase_1", "v")
        assert load_rig_config(path).rigs["worker"].env == {"MixedCase_1": "v"}
        assert rig_setting(path, "worker", "env.MixedCase_1").value == "v"
        assert rig_setting(path, "worker", "env.MIXEDCASE_1").value is None

    def test_unset_name_reports_not_set(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "claude")
        s = rig_setting(path, "worker", "env.ENABLE_PROMPT_CACHING_1H")
        assert (s.value, s.explicit, s.source, s.display()) == (
            None, False, "not set", "unset"
        )

    @pytest.mark.parametrize(
        "name", ["TELL_OUTBOX_DIR", "PWD", "R4T_CONTINUE", "R4T_NODE", "R4T_ANYTHING"]
    )
    def test_turn_owned_names_are_refused_not_silently_overridden(self, tmp_path, name):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "claude")
        with pytest.raises(RigError) as excinfo:
            set_rig_value(path, "worker", f"env.{name}", "x")
        message = str(excinfo.value)
        assert message.startswith(f"{name} belongs to the turn")
        assert "(try: r4t rig set <rig> env." in message
        assert "env" not in json.loads(path.read_text(encoding="utf-8"))["worker"]

    def test_hand_edited_turn_owned_name_fails_the_rig_closed(self, tmp_path):
        path = write_config(tmp_path, {
            "worker": {
                "invoke": ["x", "{prompt}"],
                "env": {"TELL_OUTBOX_DIR": "/nowhere/theirs"},
            },
        })
        rig = load_rig_config(path).rigs["worker"]
        assert rig.error and "TELL_OUTBOX_DIR belongs to the turn" in rig.error
        assert rig.env == {}
        assert load_rig_config(path).rig_for(member(rig="worker"))[0] is None

    @pytest.mark.parametrize("name", ["", "1BAD", "HAS SPACE", "HAS-DASH", "a=b"])
    def test_unusable_variable_names_are_refused(self, tmp_path, name):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "claude")
        with pytest.raises(RigError, match="not a usable environment variable name"):
            set_rig_value(path, "worker", f"env.{name}", "x")

    def test_parse_rejects_a_non_string_value(self, tmp_path):
        path = write_config(tmp_path, {
            "worker": {"invoke": ["x", "{prompt}"], "env": {"CACHE": 1}},
        })
        rig = load_rig_config(path).rigs["worker"]
        assert "env.CACHE: expected a string, got 1" in rig.error

    def test_parse_rejects_a_non_object_map(self, tmp_path):
        path = write_config(tmp_path, {
            "worker": {"invoke": ["x", "{prompt}"], "env": ["CACHE=1"]},
        })
        assert "env: expected an object" in load_rig_config(path).rigs["worker"].error

    def test_a_set_value_is_always_a_string(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "claude")
        set_rig_value(path, "worker", "env.CACHE", 1)
        assert json.loads(path.read_text(encoding="utf-8"))["worker"]["env"] == {
            "CACHE": "1"
        }

    def test_get_lists_only_the_entries_the_rig_carries(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "claude")
        assert [s.key for s in rig_settings(path, "worker")] == list(
            CONFIGURABLE_RIG_KEYS
        )
        set_rig_value(path, "worker", "env.ZED", "2")
        set_rig_value(path, "worker", "env.ABLE", "1")
        rows = rig_settings(path, "worker")
        assert [s.key for s in rows[len(CONFIGURABLE_RIG_KEYS):]] == [
            "env.ABLE", "env.ZED"
        ]

    def test_unknown_setting_error_advertises_the_env_shape(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "claude")
        with pytest.raises(RigError, match=r"env\.<NAME>"):
            set_rig_value(path, "worker", "env", "1")

    def test_cli_set_get_unset(self, tmp_path, capsys):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "claude")
        args = ["--rig-config", str(path)]
        assert r4t_main(
            ["rig", "set", "worker", "env.ENABLE_PROMPT_CACHING_1H", "1", *args]
        ) == 0
        assert "set worker env.ENABLE_PROMPT_CACHING_1H = 1" in capsys.readouterr().out
        assert r4t_main(
            ["rig", "get", "worker", "env.ENABLE_PROMPT_CACHING_1H", *args]
        ) == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == "1"
        assert captured.err.strip() == "(explicit)"
        assert r4t_main(["rig", "get", "worker", *args]) == 0
        assert "env.ENABLE_PROMPT_CACHING_1H  1  (explicit)" in capsys.readouterr().out
        assert r4t_main(
            ["rig", "unset", "worker", "env.ENABLE_PROMPT_CACHING_1H", *args]
        ) == 0
        assert "unset worker env.ENABLE_PROMPT_CACHING_1H" in capsys.readouterr().out
        assert "env" not in json.loads(path.read_text(encoding="utf-8"))["worker"]

    def test_cli_refuses_a_turn_owned_name_action_first(self, tmp_path, capsys):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "worker", "claude")
        assert r4t_main(
            ["rig", "set", "worker", "env.R4T_NODE", "x", "--rig-config", str(path)]
        ) == 1
        err = capsys.readouterr().err
        assert err.startswith("R4T_NODE belongs to the turn") and "(try:" in err


class TestKnowledgeTiers:
    """The rig-tier defaults for the Knowledge inject budget (#52) — a
    harness-class table separate from `text_tier`, since a fast small-effort
    agy model and a big-context codex/claude model earn different tiers."""

    def test_every_preset_lands_in_exactly_one_tier(self):
        tiered = KNOWLEDGE_TIER_LOW | KNOWLEDGE_TIER_MID | KNOWLEDGE_TIER_HIGH
        assert tiered <= set(preset_names())
        assert (
            len(KNOWLEDGE_TIER_LOW) + len(KNOWLEDGE_TIER_MID) + len(KNOWLEDGE_TIER_HIGH)
            == len(tiered)
        )

    def test_tier_bytes_are_anchored_on_knowledge_sizes(self):
        assert knowledge_tier_bytes("claude") == KNOWLEDGE_SIZES["large"]
        assert knowledge_tier_bytes("codex") == KNOWLEDGE_SIZES["large"]
        assert knowledge_tier_bytes("agy") == KNOWLEDGE_SIZES["medium"]
        assert knowledge_tier_bytes("cursor") == KNOWLEDGE_SIZES["medium"]
        assert knowledge_tier_bytes("opencode") == KNOWLEDGE_SIZES["small"]
        assert knowledge_tier_bytes("ollama") == KNOWLEDGE_SIZES["small"]
        assert knowledge_tier_bytes("ollama-opencode") == KNOWLEDGE_SIZES["small"]

    def test_unknown_or_absent_preset_gets_the_global_floor(self):
        assert knowledge_tier_bytes(None) == KNOWLEDGE_DEFAULT_BUDGET
        assert knowledge_tier_bytes("some-custom-cli") == KNOWLEDGE_DEFAULT_BUDGET
        assert KNOWLEDGE_DEFAULT_BUDGET == KNOWLEDGE_SIZES["small"]

    def test_floor_flags_only_the_low_tier(self):
        assert is_below_knowledge_floor("ollama") is True
        assert is_below_knowledge_floor("opencode") is True
        assert is_below_knowledge_floor("ollama-claude") is True
        assert is_below_knowledge_floor("agy") is False
        assert is_below_knowledge_floor("cursor") is False
        assert is_below_knowledge_floor("claude") is False
        assert is_below_knowledge_floor(None) is False


class TestResolveKnowledgeBytes:
    """Resolution order for the effective inject budget: member explicit size
    > rig-tier default > global default. `rig` here is always the member's
    OWN turn rig — inject rides the harness that wakes the member."""

    def test_off_is_zero_whatever_the_rig(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {
            "big": {"invoke": ["claude", "{prompt}"], "preset": "claude"},
        }))
        m = Member(name="Wren", rig="big")
        assert resolve_knowledge_bytes(m, config.rigs["big"]) == 0

    def test_member_explicit_size_wins_over_rig_tier(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {
            "big": {"invoke": ["claude", "{prompt}"], "preset": "claude"},
        }))
        m = Member(name="Wren", rig="big", knowledge_on=True, knowledge_bytes=999)
        assert resolve_knowledge_bytes(m, config.rigs["big"]) == 999

    def test_bare_on_takes_the_rig_tier_default(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {
            "local": {"invoke": ["ollama", "run", "{prompt}"], "preset": "ollama"},
        }))
        m = Member(name="Wren", rig="local", knowledge_on=True)
        assert resolve_knowledge_bytes(m, config.rigs["local"]) == KNOWLEDGE_SIZES["small"]

    def test_no_rig_falls_back_to_the_global_default(self):
        m = Member(name="Wren", rig="ghost", knowledge_on=True)
        assert resolve_knowledge_bytes(m, None) == KNOWLEDGE_DEFAULT_BUDGET


class TestFramingRigDefault:
    """The rig-level `framing` key (#62): same three forms as the roster
    line (roster.parse_framing), parsed unquoted here since a rigs.json
    string is already delimited — no "off"/"default" collision to guard."""

    def test_absent_is_none(self, tmp_path):
        path = write_config(tmp_path, {"big": {"invoke": ["claude", "{prompt}"]}})
        assert load_rig_config(path).rigs["big"].framing is None

    def test_off_and_default_parse(self, tmp_path):
        path = write_config(tmp_path, {
            "off-rig": {"invoke": ["x", "{prompt}"], "framing": "off"},
            "def-rig": {"invoke": ["x", "{prompt}"], "framing": "default"},
        })
        rigs = load_rig_config(path).rigs
        assert rigs["off-rig"].framing == FramingSpec(off=True)
        assert rigs["def-rig"].framing == FramingSpec()

    def test_custom_text_needs_no_quotes(self, tmp_path):
        path = write_config(tmp_path, {
            "custom": {"invoke": ["x", "{prompt}"], "framing": "background only, verify"},
        })
        rig = load_rig_config(path).rigs["custom"]
        assert rig.framing == FramingSpec(text="background only, verify")

    def test_non_string_is_an_error(self, tmp_path):
        path = write_config(tmp_path, {"bad": {"invoke": ["x", "{prompt}"], "framing": 5}})
        rig = load_rig_config(path).rigs["bad"]
        assert "framing: expected a string, got 5" in rig.error


class TestResolveFraming:
    """Resolution order for the effective Framing spec: member explicit
    line > rig config default > the built-in (unset FramingSpec)."""

    def test_member_explicit_wins_over_rig_default(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {
            "big": {"invoke": ["claude", "{prompt}"], "framing": "off"},
        }))
        m = Member(name="Wren", rig="big", framing=FramingSpec(text="mine"))
        assert resolve_framing(m, config.rigs["big"]) == FramingSpec(text="mine")

    def test_rig_default_applies_when_member_silent(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {
            "big": {"invoke": ["claude", "{prompt}"], "framing": "off"},
        }))
        m = Member(name="Wren", rig="big")
        assert resolve_framing(m, config.rigs["big"]) == FramingSpec(off=True)

    def test_no_rig_falls_back_to_built_in(self):
        m = Member(name="Wren", rig="ghost")
        assert resolve_framing(m, None) == FramingSpec()

    def test_rig_with_no_explicit_default_falls_back_to_built_in(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {
            "big": {"invoke": ["claude", "{prompt}"]},
        }))
        m = Member(name="Wren", rig="big")
        assert resolve_framing(m, config.rigs["big"]) == FramingSpec()


class TestDistillCommand:
    """`Rig.distill_command` turns a rig's own invoke into the stdin->stdout
    shell command k7e's `K7E_DISTILL_COMMAND` expects (#52). k7e pipes the
    prompt to the command's stdin with no shell of its own, and not every
    harness reads stdin as its prompt (agy prints usage instead), so a
    `{prompt}` token becomes `"$(cat)"` under an `sh -c` wrapper — the prompt
    lands in the invoke's own argument position."""

    def inner(self, cmd):
        parts = shlex.split(cmd)
        assert parts[:2] == ["sh", "-c"] and len(parts) == 3
        return parts[2]

    def test_prompt_token_becomes_cat_substitution(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {
            "local": {"invoke": ["ollama", "run", "qwen3:1.7b", "{prompt}"], "preset": "ollama"},
        }))
        cmd = config.rigs["local"].distill_command(tmp_path)
        assert self.inner(cmd) == 'ollama run qwen3:1.7b "$(cat)"'

    def test_embedded_prompt_token_concatenates(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {
            "flag": {"invoke": ["mycli", "--prompt={prompt}"]},
        }))
        cmd = config.rigs["flag"].distill_command(tmp_path)
        assert self.inner(cmd) == 'mycli --prompt="$(cat)"'

    def test_fills_the_workdir_placeholder(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {
            "code": {
                "invoke": ["opencode", "run", "--auto", "--dir", "{workdir}", "{prompt}"],
                "preset": "opencode",
            },
        }))
        home = tmp_path / "store"
        cmd = config.rigs["code"].distill_command(home)
        assert self.inner(cmd) == f'opencode run --auto --dir {home} "$(cat)"'

    def test_quotes_argv_tokens_that_need_it(self, tmp_path):
        config = load_rig_config(write_config(tmp_path, {
            "local": {"invoke": ["my cli", "{prompt}"]},
        }))
        cmd = config.rigs["local"].distill_command(tmp_path)
        assert self.inner(cmd) == '\'my cli\' "$(cat)"'

    def test_empty_rig_returns_none(self):
        assert Rig(name="empty").distill_command(Path(".")) is None

    def test_agy_resolves_the_live_model_before_bridging(self, tmp_path, monkeypatch):
        config = load_rig_config(write_config(tmp_path, {
            "brain": {
                "invoke": ["agy", "--model", "{model}", "--print", "{prompt}"],
                "preset": "agy", "model": "flash", "model_resolver": "agy-live",
            },
        }))
        monkeypatch.setattr(
            rig_module, "resolve_agy_model", lambda query, **k: "Gemini 3.6 Flash Low"
        )
        cmd = config.rigs["brain"].distill_command(tmp_path)
        assert self.inner(cmd) == "agy --model 'Gemini 3.6 Flash Low' --print \"$(cat)\""

    def test_agy_unresolved_model_returns_none(self, tmp_path, monkeypatch):
        config = load_rig_config(write_config(tmp_path, {
            "brain": {
                "invoke": ["agy", "--model", "{model}", "--print", "{prompt}"],
                "preset": "agy", "model": "nope", "model_resolver": "agy-live",
            },
        }))

        def boom(*a, **k):
            raise RigError("no match")

        monkeypatch.setattr(rig_module, "resolve_agy_model", boom)
        assert config.rigs["brain"].distill_command(tmp_path) is None
