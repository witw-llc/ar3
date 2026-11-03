"""The engine access knobs: `add_dirs`, `allow_tools`, `deny_tools`, and the
node-wide `deny_floor`.

Argv shapes are checked against the flags each CLI's own `--help` lists; what
the CLI then does with them is the live tests' business, not this file's.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import engines.run as engine_run
import rig as rig_mod
import runbook
import transcript
from conftest import launch_for, write_path_executable
from rig import (
    ACCESS_TRANSLATION,
    PERMISSION_TRANSLATION,
    RigError,
    access_binary_state,
    apply_access,
    build_preset_invoke,
    engine_home,
    load_deny_floor,
    load_rig_config,
    normalize_add_dirs,
    parse_access_list,
    rig_from_spec,
    set_rig_value,
    swap_preset_rig,
    unset_rig_value,
)
from roster import Member

COPILOT_DENY = "shell(git push)"


@pytest.fixture(autouse=True)
def _stub_claude_on_path(tmp_path_factory, monkeypatch):
    """No test here may reach a real CLI: a turn that spawns finds this stub,
    which reports the verified version and does nothing else."""
    bin_dir = tmp_path_factory.mktemp("stub-bin")
    fake_binary(bin_dir, "claude", "2.1.316 (Claude Code)")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))


def invoke(preset: str) -> list[str]:
    return build_preset_invoke(preset, model="m" if preset.startswith("ollama-") else None)


def write_rigs(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "rigs.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def member_with(rig: str | None = None, override=None) -> Member:
    member = Member(name="Lead", rig=rig)
    member.rig_override = override
    return member


# --- grammar ------------------------------------------------------------------


class TestGrammar:
    @pytest.mark.parametrize(
        "text",
        [
            "shell(git",
            "a)b",
            "a\rb",
            "a\nb",
            "a\x00b",
            "a\tb",
            "a\x7fb",
            "-x",
            "shell(rm), --yolo",
        ],
    )
    def test_a_malformed_list_is_refused_and_nothing_is_dropped(self, text):
        with pytest.raises(RigError):
            parse_access_list(text, "deny_tools")

    def test_empty_entries_are_dropped_and_commas_in_parentheses_stay(self):
        assert parse_access_list(",,", "allow_tools") == []
        assert parse_access_list("", "allow_tools") == []
        assert parse_access_list(
            " shell(a,b) , url(c.example) ,, shell(a,b)", "allow_tools"
        ) == ["shell(a,b)", "url(c.example)"]

    def test_order_is_kept_and_the_first_duplicate_wins(self):
        assert parse_access_list("c,a,b,a,c", "add_dirs") == ["c", "a", "b"]

    def test_a_top_level_comma_cannot_be_expressed_but_a_nested_one_can(self):
        assert parse_access_list("shell(a,b)", "deny_tools") == ["shell(a,b)"]

    def test_a_dash_inside_an_entry_is_fine(self):
        assert parse_access_list("shell(git-lfs push)", "deny_tools") == ["shell(git-lfs push)"]


class TestAddDirs:
    def test_a_leading_tilde_expands_against_the_childs_home(self, tmp_path):
        home = tmp_path / "home"
        (home / "shared").mkdir(parents=True)
        assert normalize_add_dirs(["~/shared"], home=str(home)) == [str(home / "shared")]

    @pytest.mark.parametrize("entry", ["relative/dir", "a/~/b", "~other/x", "~"])
    def test_a_relative_or_non_leading_tilde_path_is_refused(self, tmp_path, entry):
        with pytest.raises(RigError, match="relative path"):
            normalize_add_dirs([entry], home=str(tmp_path))

    def test_a_file_is_refused(self, tmp_path):
        target = tmp_path / "f.txt"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(RigError, match="not a directory"):
            normalize_add_dirs([str(target)], home=str(tmp_path))

    def test_a_missing_directory_is_refused(self, tmp_path):
        with pytest.raises(RigError, match="not a directory"):
            normalize_add_dirs([str(tmp_path / "gone")], home=str(tmp_path))

    @pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
    def test_a_symlink_to_a_directory_is_accepted(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert normalize_add_dirs([str(link)], home=str(tmp_path)) == [str(link)]

    def test_entries_dedupe_after_normalization_in_order(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        got = normalize_add_dirs(
            [str(b), f"{a}/.", str(a), f"{b}/../b"], home=str(tmp_path)
        )
        assert got == [str(b), str(a)]


# --- the translation table ----------------------------------------------------


class TestMatrix:
    def test_claude(self):
        argv = apply_access(
            invoke("claude"), "claude",
            add_dirs=["/srv/a", "/srv/b"], allow_tools=["Read(/srv/a/**)"],
            deny_tools=["Bash(git push:*)", "Bash(rm:*)"],
        )
        assert argv[:5] == ["claude", "--add-dir", "/srv/a", "--add-dir", "/srv/b"]
        # The allow rules ride the preset's own `--allowedTools` element, so a
        # deny and an allow never sit in the same list.
        held = argv[argv.index("--allowedTools") + 1]
        assert held.endswith(",Read(/srv/a/**)")
        assert argv.count("--allowedTools") == 1
        assert argv[argv.index("--disallowedTools") + 1] == "Bash(git push:*),Bash(rm:*)"
        assert argv[-2:] == ["-p", "{prompt}"]

    def test_claude_without_an_allowed_list_gets_its_own_element(self):
        argv = apply_access(["claude", "-p", "{prompt}"], "claude", allow_tools=["Read"])
        assert argv == ["claude", "--allowedTools", "Read", "-p", "{prompt}"]

    def test_copilot_takes_the_equals_form_one_flag_per_rule(self):
        argv = apply_access(
            invoke("copilot"), "copilot",
            add_dirs=["/srv/a", "/srv/b"],
            allow_tools=["url(example.org)", "url(example.net)"],
            deny_tools=["shell(rm)", "shell(git push)"],
        )
        assert argv[1:9] == [
            "--add-dir=/srv/a", "--add-dir=/srv/b",
            "--allow-tool=url(example.org)", "--allow-tool=url(example.net)",
            "--deny-tool=shell(rm)", "--deny-tool=shell(git push)",
            "--allow-all-tools", "-s",
        ]

    def test_codex_add_dir_lands_after_exec(self):
        argv = apply_access(invoke("codex"), "codex", add_dirs=["/srv/a"])
        assert argv[:4] == ["codex", "exec", "--add-dir", "/srv/a"]

    @pytest.mark.parametrize("preset", ["cursor", "agy"])
    def test_cursor_and_agy_take_add_dir_only(self, preset):
        argv = apply_access(invoke(preset), preset, add_dirs=["/srv/a"])
        assert argv[1:3] == ["--add-dir", "/srv/a"]

    @pytest.mark.parametrize("preset", ["codex", "cursor", "agy"])
    @pytest.mark.parametrize("knob", ["allow_tools", "deny_tools"])
    def test_those_engines_refuse_the_rule_knobs_with_the_engines_that_take_them(
        self, preset, knob
    ):
        with pytest.raises(RigError) as caught:
            apply_access(invoke(preset), preset, **{knob: ["x"]})
        text = str(caught.value)
        assert "deferred" in text and "claude" in text and "copilot" in text

    @pytest.mark.parametrize("preset", ["opencode", "devin", "muse", "ollama", "ollama-opencode"])
    @pytest.mark.parametrize("knob", ["add_dirs", "allow_tools", "deny_tools"])
    def test_every_other_engine_refuses_every_knob_before_the_turn(self, preset, knob):
        with pytest.raises(RigError) as caught:
            apply_access(["x", "{prompt}"], preset, **{knob: ["/srv/a"]})
        assert f"preset {preset!r} takes no" in str(caught.value)

    def test_a_preset_the_table_does_not_know_refuses(self):
        with pytest.raises(RigError, match="no known preset"):
            apply_access(["x"], None, deny_tools=["a"])

    def test_empty_lists_change_nothing(self):
        argv = invoke("copilot")
        assert apply_access(argv, "copilot") == argv
        assert apply_access(argv, "opencode") == argv

    @pytest.mark.parametrize(
        "preset,wrapped",
        [("ollama-claude", "claude"), ("ollama-codex", "codex"), ("ollama-copilot", "copilot")],
    )
    def test_a_launcher_takes_the_wrapped_engines_flags_after_its_anchor(self, preset, wrapped):
        argv = apply_access(invoke(preset), preset, add_dirs=["/srv/a"])
        anchor = ACCESS_TRANSLATION[preset].anchor
        at = argv.index(anchor)
        assert "--add-dir" in argv[at:] or "--add-dir=/srv/a" in argv[at:]
        assert "--add-dir" not in argv[:at] and "--add-dir=/srv/a" not in argv[:at]
        assert rig_mod.access_engine(preset) == wrapped

    def test_the_table_agrees_with_the_permission_anchors(self):
        for preset, entry in ACCESS_TRANSLATION.items():
            if preset in PERMISSION_TRANSLATION:
                assert entry.anchor == PERMISSION_TRANSLATION[preset].anchor, preset

    def test_codex_refuses_add_dirs_with_continue(self):
        with pytest.raises(RigError, match="exec resume"):
            rig_mod.check_access_support(
                "codex", add_dirs=["/srv/a"], continue_conversation=True
            )
        # No add_dirs, no conflict; another engine, no conflict.
        rig_mod.check_access_support("codex", continue_conversation=True)
        rig_mod.check_access_support("cursor", add_dirs=["/srv/a"], continue_conversation=True)

    def test_a_roster_member_with_continue_on_codex_and_add_dirs_fails_closed(self, tmp_path):
        spec = {"preset": "codex", "invoke": invoke("codex"), "add_dirs": "/tmp"}
        member = member_with(override=rig_from_spec("cx", spec))
        member.continue_conversation = True
        _rig, err, _ = load_rig_config(tmp_path / "none.json").rig_for(member)
        assert err and "exec resume" in err

    def test_the_permission_drop_lists_never_hold_a_deny_flag(self):
        deny_flags = {
            entry.deny_tool for entry in ACCESS_TRANSLATION.values() if entry.deny_tool
        }
        assert deny_flags == {"--disallowedTools", "--deny-tool"}
        for table in PERMISSION_TRANSLATION.values():
            for rule in table.modes.values():
                assert not deny_flags & set(rule.drop)
                assert not deny_flags & set(rule.drop_pair)


# --- the binary and its version -------------------------------------------------


def fake_binary(directory: Path, name: str, version_line: str) -> Path:
    return write_path_executable(
        directory, name,
        "import sys\n"
        "if '--version' in sys.argv:\n"
        f"    print({version_line!r})\n",
    )


class TestVersion:
    def env(self, tmp_path, version_line):
        fake_binary(tmp_path / "bin", "claude", version_line)
        return {"PATH": str(tmp_path / "bin") + os.pathsep + os.environ.get("PATH", "")}

    def test_the_verified_build_is_recorded_with_its_path(self, tmp_path):
        state = access_binary_state("claude", launch_for("claude", self.env(tmp_path, "2.1.316 (Claude Code)")))
        assert state["status"] == "verified"
        assert state["version"] == "2.1.316"
        assert state["binary"].startswith(str(tmp_path / "bin"))

    def test_a_newer_build_runs_and_is_recorded_unverified(self, tmp_path):
        state = access_binary_state("claude", launch_for("claude", self.env(tmp_path, "2.2.0 (Claude Code)")))
        assert state["status"] == "unverified 2.2.0"

    def test_an_older_build_refuses_the_knobs(self, tmp_path):
        with pytest.raises(RigError, match="older than 2.1.316"):
            access_binary_state("claude", launch_for("claude", self.env(tmp_path, "2.1.100 (Claude Code)")))

    def test_an_unreadable_version_runs_and_is_recorded_unverified(self, tmp_path):
        state = access_binary_state("claude", launch_for("claude", self.env(tmp_path, "no digits here")))
        assert state["status"] == "unverified unknown"

    def test_a_missing_binary_is_unverified_not_a_crash(self, tmp_path):
        state = access_binary_state("claude", launch_for("claude", {"PATH": str(tmp_path)}))
        assert state["binary"] is None and state["status"] == "unverified unknown"

    def test_cursor_compares_its_date_version(self, tmp_path):
        fake_binary(tmp_path / "bin", "agent", "2026.10.22-27dcf5f")
        env = {"PATH": str(tmp_path / "bin")}
        assert access_binary_state("cursor", launch_for("agent", env))["status"] == "verified"
        fake_binary(tmp_path / "bin", "agent", "2026.09.01-abc")
        with pytest.raises(RigError, match="older than"):
            access_binary_state("cursor", launch_for("agent", env))


# --- layers and the deny composition --------------------------------------------


class TestRigKeys:
    def test_a_rig_loads_the_lists_normalized(self, tmp_path):
        config = load_rig_config(write_rigs(tmp_path, {
            "r": {"preset": "copilot", "invoke": invoke("copilot"),
                  "add_dirs": "/tmp, /tmp", "allow_tools": ["url(a.example),url(b.example)"],
                  "deny_tools": "shell(rm)"},
        }))
        rig = config.rigs["r"]
        assert not rig.error
        assert (rig.add_dirs, rig.allow_tools, rig.deny_tools) == (
            ["/tmp"], ["url(a.example)", "url(b.example)"], ["shell(rm)"]
        )

    def test_a_knob_the_engine_lacks_fails_the_rig_closed(self, tmp_path):
        config = load_rig_config(write_rigs(tmp_path, {
            "r": {"preset": "devin", "invoke": invoke("devin"), "deny_tools": "x"},
        }))
        rig, err, _ = config.rig_for(member_with("r"))
        assert rig is None and "takes no --deny-tool" in err

    def test_a_malformed_list_fails_the_rig_closed(self, tmp_path):
        config = load_rig_config(write_rigs(tmp_path, {
            "r": {"preset": "copilot", "invoke": invoke("copilot"), "allow_tools": "url(a"},
        }))
        assert "unbalanced" in config.rigs["r"].error

    def test_the_rig_argv_spells_the_lists_after_the_legacy_allowlist(self, tmp_path):
        config = load_rig_config(write_rigs(tmp_path, {
            "r": {"preset": "claude", "invoke": invoke("claude"),
                  "allowed_tools": "Read Edit", "allow_tools": "Bash(ls:*)",
                  "deny_tools": "Bash(rm:*)"},
        }))
        argv = config.rigs["r"].argv("hi")
        # `allowed_tools` replaces the list first; `allow_tools` is appended to it.
        assert argv[argv.index("--allowedTools") + 1] == "Read Edit,Bash(ls:*)"
        assert argv[argv.index("--disallowedTools") + 1] == "Bash(rm:*)"

    def test_the_legacy_allowlist_never_touches_a_deny_flag(self, tmp_path):
        config = load_rig_config(write_rigs(tmp_path, {
            "r": {"preset": "claude", "invoke": invoke("claude"),
                  "allowed_tools": "Read", "deny_tools": "Bash(rm:*)"},
        }))
        argv = config.rigs["r"].argv("hi")
        assert "--disallowedTools" in argv and argv[argv.index("--allowedTools") + 1] == "Read"

    @pytest.mark.parametrize("stance", ["auto", "bypass"])
    @pytest.mark.parametrize("preset", ["claude", "copilot"])
    def test_a_deny_survives_every_stance(self, tmp_path, preset, stance):
        config = load_rig_config(write_rigs(tmp_path, {
            "r": {"preset": preset, "invoke": invoke(preset), "permissions": stance,
                  "deny_tools": "shell(rm)"},
        }))
        argv = config.rigs["r"].argv("hi")
        flag = "--disallowedTools" if preset == "claude" else "--deny-tool=shell(rm)"
        assert flag in argv

    def test_a_rule_both_allowed_and_denied_is_emitted_on_both_sides(self, tmp_path):
        config = load_rig_config(write_rigs(tmp_path, {
            "r": {"preset": "copilot", "invoke": invoke("copilot"),
                  "allow_tools": "shell(git push)", "deny_tools": "shell(git push)"},
        }))
        argv = config.rigs["r"].argv("hi")
        assert "--allow-tool=shell(git push)" in argv and "--deny-tool=shell(git push)" in argv

    def test_unset_layers_add_nothing(self, tmp_path):
        config = load_rig_config(write_rigs(tmp_path, {
            "r": {"preset": "copilot", "invoke": invoke("copilot")},
        }))
        assert config.rigs["r"].argv("hi") == build_preset_invoke("copilot")[:-1] + ["hi"]


class TestDenyFloor:
    def floor_config(self, tmp_path, **rigs):
        return load_rig_config(write_rigs(tmp_path, {
            "deny_floor": {"copilot": [COPILOT_DENY]}, **rigs,
        }))

    def test_the_floor_is_unioned_with_the_rigs_denies_never_replaced(self, tmp_path):
        config = self.floor_config(tmp_path, r={
            "preset": "copilot", "invoke": invoke("copilot"), "deny_tools": "shell(rm)",
        })
        rig, err, _ = config.rig_for(member_with("r"))
        assert err is None
        assert rig.deny_tools == [COPILOT_DENY, "shell(rm)"]
        assert rig.argv("hi").count(f"--deny-tool={COPILOT_DENY}") == 1

    def test_a_rig_with_no_denies_still_carries_the_floor(self, tmp_path):
        config = self.floor_config(tmp_path, r={"preset": "copilot", "invoke": invoke("copilot")})
        rig, _, _ = config.rig_for(member_with("r"))
        assert f"--deny-tool={COPILOT_DENY}" in rig.argv("hi")

    def test_the_floor_holds_under_bypass(self, tmp_path):
        config = self.floor_config(tmp_path, r={
            "preset": "copilot", "invoke": invoke("copilot"), "permissions": "bypass",
        })
        rig, _, _ = config.rig_for(member_with("r"))
        argv = rig.argv("hi")
        assert "--allow-all" in argv and f"--deny-tool={COPILOT_DENY}" in argv

    def test_an_inline_member_gets_the_floor(self, tmp_path):
        config = self.floor_config(tmp_path)
        inline = rig_from_spec("Lead", runbook.parse_engine_line("copilot"))
        rig, err, _ = config.rig_for(member_with(override=inline))
        assert err is None and rig.deny_tools == [COPILOT_DENY]

    def test_a_launcher_gets_the_wrapped_engines_floor(self, tmp_path):
        config = self.floor_config(tmp_path, r={
            "preset": "ollama-copilot", "invoke": invoke("ollama-copilot"),
        })
        rig, err, _ = config.rig_for(member_with("r"))
        assert err is None
        argv = rig.argv("hi")
        assert f"--deny-tool={COPILOT_DENY}" in argv[argv.index("--"):]

    def test_another_engines_floor_does_not_leak(self, tmp_path):
        config = self.floor_config(tmp_path, r={"preset": "claude", "invoke": invoke("claude")})
        rig, _, _ = config.rig_for(member_with("r"))
        assert rig.deny_tools == [] and "--disallowedTools" not in rig.argv("hi")

    def test_a_custom_invoke_rig_that_runs_a_floored_cli_is_refused(self, tmp_path):
        config = self.floor_config(tmp_path, r={"invoke": ["copilot", "-p", "{prompt}"]})
        rig, err, _ = config.rig_for(member_with("r"))
        assert rig is None and "floor" in err

    def test_a_floor_for_an_engine_with_no_deny_flag_is_refused_when_the_file_loads(self, tmp_path):
        path = write_rigs(tmp_path, {"deny_floor": {"codex": ["x"]}})
        with pytest.raises(RigError, match="takes no --deny-tool"):
            load_rig_config(path)

    def test_a_floor_must_name_the_engine_not_its_launcher(self, tmp_path):
        path = write_rigs(tmp_path, {"deny_floor": {"ollama-copilot": ["x"]}})
        with pytest.raises(RigError, match="name the engine it runs: copilot"):
            load_rig_config(path)

    def test_a_floor_that_is_not_an_object_is_refused(self, tmp_path):
        with pytest.raises(RigError, match="object"):
            load_rig_config(write_rigs(tmp_path, {"deny_floor": ["x"]}))

    def test_a_malformed_floor_rule_is_refused(self, tmp_path):
        with pytest.raises(RigError, match="unbalanced"):
            load_rig_config(write_rigs(tmp_path, {"deny_floor": {"copilot": "shell(rm"}}))

    def test_load_deny_floor_reads_only_the_floor_and_refuses_a_broken_file(self, tmp_path):
        assert load_deny_floor(tmp_path / "absent.json") == {}
        assert load_deny_floor(write_rigs(tmp_path, {"x": 1})) == {}
        assert load_deny_floor(
            write_rigs(tmp_path, {"deny_floor": {"claude": "Bash(rm:*)"}})
        ) == {"claude": ["Bash(rm:*)"]}
        broken = tmp_path / "broken.json"
        broken.write_text("{", encoding="utf-8")
        with pytest.raises(RigError, match="cannot load"):
            load_deny_floor(broken)


class TestShadowCarry:
    def machine(self, tmp_path, preset="claude", deny="Bash(rm:*)", **extra):
        return load_rig_config(write_rigs(tmp_path, {
            "lead": {"preset": preset, "invoke": invoke(preset), "deny_tools": deny, **extra},
        }))

    def runbook_rig(self, preset, **spec):
        return rig_from_spec("lead", {"preset": preset, "invoke": invoke(preset), **spec})

    def test_a_runbook_rig_that_shadows_a_machine_rig_keeps_its_denies(self, tmp_path):
        machine = self.machine(tmp_path)
        shadow = self.runbook_rig("claude", deny_tools="Bash(curl:*)")
        rig, err, _ = machine.rig_for(member_with("lead", shadow))
        assert err is None
        assert rig.deny_tools == ["Bash(rm:*)", "Bash(curl:*)"]

    def test_a_shadow_with_no_denies_of_its_own_still_carries_them(self, tmp_path):
        rig, err, _ = self.machine(tmp_path).rig_for(
            member_with("lead", self.runbook_rig("claude"))
        )
        assert err is None and rig.deny_tools == ["Bash(rm:*)"]

    def test_the_rest_of_the_shadow_stays_whole_block(self, tmp_path):
        machine = self.machine(tmp_path, permissions="auto", allow_tools="Read")
        rig, _, _ = machine.rig_for(member_with("lead", self.runbook_rig("claude")))
        assert rig.allow_tools == [] and rig.permissions is None

    def test_a_shadow_that_runs_a_different_engine_refuses_the_carried_denies(self, tmp_path):
        rig, err, _ = self.machine(tmp_path).rig_for(
            member_with("lead", self.runbook_rig("copilot"))
        )
        assert rig is None and "never translated or dropped" in err

    def test_a_shadow_on_another_engine_is_fine_when_there_is_nothing_to_carry(self, tmp_path):
        machine = load_rig_config(write_rigs(tmp_path, {
            "lead": {"preset": "claude", "invoke": invoke("claude")},
        }))
        rig, err, _ = machine.rig_for(member_with("lead", self.runbook_rig("copilot")))
        assert err is None and rig.preset == "copilot"

    def test_an_unreadable_machine_deny_list_is_not_carried_as_nothing(self, tmp_path):
        machine = self.machine(tmp_path, deny="Bash(rm")
        rig, err, _ = machine.rig_for(member_with("lead", self.runbook_rig("claude")))
        assert rig is None and "cannot be carried" in err

    def test_a_wrapper_over_the_same_engine_carries_the_denies(self, tmp_path):
        machine = self.machine(tmp_path)
        rig, err, _ = machine.rig_for(
            member_with("lead", self.runbook_rig("ollama-claude"))
        )
        assert err is None and rig.deny_tools == ["Bash(rm:*)"]

    def test_a_deny_does_not_survive_a_rename_by_design(self, tmp_path):
        machine = self.machine(tmp_path)
        other = rig_from_spec("other", {"preset": "claude", "invoke": invoke("claude")})
        rig, err, _ = machine.rig_for(member_with(override=other))
        assert err is None and rig.deny_tools == []


class TestCliSurface:
    def test_the_invocation_lists_are_parsed_per_flag_and_unioned(self):
        import argparse

        import r4t

        args = argparse.Namespace(
            add_dir=["/a,/b", "/a"], allow_tool=["url(x),url(y)"], deny_tool=None
        )
        assert r4t._access_options(args) == {
            "add_dirs": ["/a", "/b"], "allow_tools": ["url(x)", "url(y)"], "deny_tools": [],
        }

    def test_one_flags_open_parenthesis_is_not_closed_by_the_next(self):
        import argparse

        import r4t

        args = argparse.Namespace(add_dir=None, allow_tool=None, deny_tool=["shell(a", "b)"])
        with pytest.raises(RigError, match="unbalanced"):
            r4t._access_options(args)

    def test_rig_set_validates_and_stores_the_list_normalized(self, tmp_path):
        path = write_rigs(tmp_path, {"r": {"preset": "copilot", "invoke": invoke("copilot")}})
        set_rig_value(path, "r", "deny_tools", "shell(rm), shell(rm),shell(git push)")
        assert json.loads(path.read_text())["r"]["deny_tools"] == "shell(rm),shell(git push)"
        with pytest.raises(RigError, match="unbalanced"):
            set_rig_value(path, "r", "allow_tools", "url(x")
        assert unset_rig_value(path, "r", "deny_tools") is True
        assert "deny_tools" not in json.loads(path.read_text())["r"]

    def test_rig_set_refuses_a_knob_the_engine_lacks(self, tmp_path):
        path = write_rigs(tmp_path, {"r": {"preset": "devin", "invoke": invoke("devin")}})
        with pytest.raises(RigError, match="takes no --add-dir"):
            set_rig_value(path, "r", "add_dirs", "/tmp")

    def test_rig_set_refuses_an_empty_list(self, tmp_path):
        path = write_rigs(tmp_path, {"r": {"preset": "copilot", "invoke": invoke("copilot")}})
        with pytest.raises(RigError, match="empty"):
            set_rig_value(path, "r", "deny_tools", ",,")

    def test_swap_to_another_engine_is_refused_while_native_rules_are_saved(self, tmp_path):
        path = write_rigs(tmp_path, {"r": {
            "preset": "claude", "invoke": invoke("claude"), "deny_tools": "Bash(rm:*)",
        }})
        with pytest.raises(RigError, match="rules and preset 'copilot'"):
            swap_preset_rig(path, "r", "copilot")
        path2 = write_rigs(tmp_path, {"r": {
            "preset": "copilot", "invoke": invoke("copilot"), "allow_tools": "url(x.example)",
        }})
        with pytest.raises(RigError, match="allow_tools"):
            swap_preset_rig(path2, "r", "claude")

    def test_swap_within_the_same_wrapped_engine_keeps_the_rules(self, tmp_path):
        path = write_rigs(tmp_path, {"r": {
            "preset": "claude", "invoke": invoke("claude"), "deny_tools": "Bash(rm:*)",
        }})
        swap_preset_rig(path, "r", "ollama-claude", model="m")
        assert json.loads(path.read_text())["r"]["deny_tools"] == "Bash(rm:*)"

    def test_swap_revalidates_add_dirs_against_the_new_preset(self, tmp_path):
        path = write_rigs(tmp_path, {"r": {
            "preset": "claude", "invoke": invoke("claude"), "add_dirs": "/tmp",
        }})
        with pytest.raises(RigError, match="takes no --add-dir"):
            swap_preset_rig(path, "r", "devin")
        swap_preset_rig(path, "r", "cursor")
        assert json.loads(path.read_text())["r"]["add_dirs"] == "/tmp"


# --- engine run -------------------------------------------------------------------


class TestEngineRun:
    def template(self, engine, tmp_path, **kwargs):
        return engine_run._build_argv_template(
            engine, model="m" if engine.startswith("ollama-") else None,
            timeout=900, workdir=tmp_path, **kwargs,
        )[0]

    def test_the_lists_reach_the_argv(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        argv = self.template(
            "copilot", tmp_path, add_dirs=[str(shared)], allow_tools=["url(x.example)"],
            deny_tools=["shell(rm)"],
        )
        assert f"--add-dir={shared}" in argv
        assert "--allow-tool=url(x.example)" in argv and "--deny-tool=shell(rm)" in argv

    def test_the_floor_is_unioned_with_the_invocations_denies(self, tmp_path):
        argv = self.template(
            "copilot", tmp_path, deny_tools=["shell(rm)"], deny_floor=[COPILOT_DENY]
        )
        assert argv.count(f"--deny-tool={COPILOT_DENY}") == 1
        assert "--deny-tool=shell(rm)" in argv

    def test_the_floor_alone_is_enough_to_emit_a_deny(self, tmp_path):
        argv = self.template("claude", tmp_path, deny_floor=["Bash(rm:*)"])
        assert argv[argv.index("--disallowedTools") + 1] == "Bash(rm:*)"

    def test_add_dirs_must_exist_in_the_childs_view(self, tmp_path):
        with pytest.raises(engine_run.RunError, match="not a directory"):
            self.template("claude", tmp_path, add_dirs=[str(tmp_path / "gone")])

    def test_a_tilde_entry_expands_against_the_spawned_homes(self, tmp_path):
        home = tmp_path / "h"
        (home / "lib").mkdir(parents=True)
        argv = self.template("claude", tmp_path, add_dirs=["~/lib"], env={"HOME": str(home)})
        assert argv[argv.index("--add-dir") + 1] == str(home / "lib")

    def test_an_engine_without_the_flag_refuses_before_the_turn(self, tmp_path):
        with pytest.raises(engine_run.RunError, match="takes no --deny-tool"):
            self.template("codex", tmp_path, deny_tools=["x"])
        with pytest.raises(engine_run.RunError, match="takes no --add-dir"):
            self.template("devin", tmp_path, add_dirs=[str(tmp_path)])

    def test_codex_add_dir_with_continue_is_refused(self, tmp_path):
        with pytest.raises(engine_run.RunError, match="exec resume"):
            self.template(
                "codex", tmp_path, add_dirs=[str(tmp_path)], continue_conversation=True
            )

    def test_resident_refuses_the_knobs(self, tmp_path):
        with pytest.raises(engine_run.RunError, match="--resident takes no"):
            self.template("claude", tmp_path, resident=True, deny_tools=["Bash(rm:*)"])

    def test_resident_refuses_a_non_empty_floor_even_with_no_knobs(self, tmp_path):
        with pytest.raises(engine_run.RunError, match="deny_floor"):
            self.template("claude", tmp_path, resident=True, deny_floor=["Bash(rm:*)"])

    def test_resident_with_an_empty_floor_is_untouched(self, tmp_path):
        assert self.template("claude", tmp_path, resident=True)

    def test_an_older_binary_stops_execute_before_any_spawn(self, tmp_path):
        fake_binary(tmp_path / "bin", "claude", "2.0.1 (Claude Code)")
        env = {**os.environ, "PATH": str(tmp_path / "bin") + os.pathsep + os.environ["PATH"]}
        with pytest.raises(engine_run.RunError, match="older than"):
            engine_run.execute(
                "claude", "hi", dir_path=tmp_path, model=None, agent=None, timeout=30,
                scaffold=False, env=env, deny_tools=["Bash(rm:*)"],
            )

    def test_execute_records_the_resolved_binary_and_prints_the_argv(self, tmp_path, capsys):
        fake_binary(tmp_path / "bin", "claude", "2.1.316 (Claude Code)")
        env = {**os.environ, "PATH": str(tmp_path / "bin") + os.pathsep + os.environ["PATH"]}
        record: dict = {}
        engine_run.execute(
            "claude", "hi", dir_path=tmp_path, model=None, agent=None, timeout=30,
            scaffold=False, echo=True, env=env, record=record,
            deny_floor=["Bash(rm:*)"], deny_tools=["Bash(curl:*)"],
        )
        out = capsys.readouterr()
        assert record["access"]["status"] == "verified"
        assert record["access"]["version"] == "2.1.316"
        assert "Bash(rm:*),Bash(curl:*)" in out.err
        assert "(verified)" in out.err

    def test_no_knob_records_no_access(self, tmp_path):
        record: dict = {}
        engine_run.execute(
            "claude", "hi", dir_path=tmp_path, model=None, agent=None, timeout=30,
            scaffold=False, echo=True, record=record,
        )
        assert "access" not in record


class TestCommandLine:
    def test_engine_run_applies_the_node_floor_from_the_rig_config(self, tmp_path, capsys, monkeypatch):
        import r4t

        monkeypatch.chdir(tmp_path)
        path = write_rigs(tmp_path, {"deny_floor": {"claude": "Bash(git push:*)"}})
        code = r4t.main([
            "engine", "claude", "run", "--echo", "--no-scaffold", "--rig-config", str(path),
            "--deny-tool", "Bash(rm:*)", "do it",
        ])
        out = capsys.readouterr().err
        assert code == 0
        assert "Bash(git push:*),Bash(rm:*)" in out

    def test_engine_run_refuses_resident_when_the_floor_names_the_engine(self, tmp_path, capsys, monkeypatch):
        import r4t

        monkeypatch.chdir(tmp_path)
        path = write_rigs(tmp_path, {"deny_floor": {"claude": "Bash(git push:*)"}})
        code = r4t.main([
            "engine", "claude", "run", "--resident", "--rig-config", str(path), "hi",
        ])
        assert code != 0
        assert "deny_floor" in capsys.readouterr().err

    def test_engine_run_refuses_a_floor_file_it_cannot_read(self, tmp_path, capsys, monkeypatch):
        import r4t

        monkeypatch.chdir(tmp_path)
        broken = tmp_path / "rigs.json"
        broken.write_text("{", encoding="utf-8")
        code = r4t.main(["engine", "claude", "run", "--echo", "--rig-config", str(broken), "hi"])
        assert code != 0
        assert "cannot load" in capsys.readouterr().err

    def test_a_malformed_flag_value_exits_before_the_turn(self, tmp_path, capsys, monkeypatch):
        import r4t

        monkeypatch.chdir(tmp_path)
        code = r4t.main(["engine", "claude", "run", "--echo", "--deny-tool", "Bash(rm", "hi"])
        assert code == 2
        assert "unbalanced" in capsys.readouterr().err

    def test_empty_values_from_unset_a8s_vars_are_no_entries(self, tmp_path, capsys, monkeypatch):
        import r4t

        monkeypatch.chdir(tmp_path)
        code = r4t.main([
            "engine", "claude", "run", "--echo", "--no-scaffold",
            "--add-dir=", "--allow-tool=", "--deny-tool=", "hi",
        ])
        out = capsys.readouterr().err
        assert code == 0
        assert "--add-dir" not in out and "--disallowedTools" not in out

    def test_engine_check_composes_the_knobs_and_the_floor(self, tmp_path, capsys, monkeypatch):
        import r4t

        monkeypatch.chdir(tmp_path)
        path = write_rigs(tmp_path, {"deny_floor": {"copilot": COPILOT_DENY}})
        r4t.main([
            "engine", "copilot", "check", "--json", "--rig-config", str(path),
            "--allow-tool", "url(x.example)",
        ])
        [report] = json.loads(capsys.readouterr().out)
        assert "--allow-tool=url(x.example)" in report["argv"]
        assert f"--deny-tool={COPILOT_DENY}" in report["argv"]

    def test_engine_check_rejects_a_knob_the_engine_lacks(self, tmp_path, capsys, monkeypatch):
        import r4t

        monkeypatch.chdir(tmp_path)
        code = r4t.main(["engine", "devin", "check", "--json", "--deny-tool", "x"])
        [report] = json.loads(capsys.readouterr().out)
        assert code == 1 and report["verdict"] == "rejected"
        assert "takes no --deny-tool" in report["detail"]


# --- roster turns -------------------------------------------------------------------


class TestRosterTurn:
    def fake_claude(self, tmp_path, version="2.1.316 (Claude Code)"):
        calls = tmp_path / "claude-calls"
        calls.mkdir()
        write_path_executable(
            tmp_path / "bin", "claude",
            "import json, os, sys\n"
            "if '--version' in sys.argv:\n"
            f"    print({version!r}); sys.exit(0)\n"
            f"n = len(os.listdir({str(calls)!r}))\n"
            f"open(os.path.join({str(calls)!r}, f'{{n}}.json'), 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
            "print('ran')\n",
        )
        return {**os.environ, "PATH": str(tmp_path / "bin") + os.pathsep + os.environ["PATH"]}, calls

    def rig(self, **spec):
        return rig_from_spec("lead", {"preset": "claude", "invoke": invoke("claude"), **spec})

    def test_the_lists_reach_the_spawned_argv(self, tmp_path):
        from dispatch import run_harness

        shared = tmp_path / "shared"
        shared.mkdir()
        env, calls = self.fake_claude(tmp_path)
        code, _out, _dur, _timed = run_harness(
            self.rig(add_dirs=str(shared), deny_tools="Bash(rm:*)"), "hi", tmp_path, env=env
        )
        [call] = list(calls.iterdir())
        argv = json.loads(call.read_text())
        assert code == 0
        assert argv[argv.index("--add-dir") + 1] == str(shared)
        assert argv[argv.index("--disallowedTools") + 1] == "Bash(rm:*)"

    def test_a_missing_directory_fails_the_turn_before_it_spawns(self, tmp_path):
        from dispatch import run_harness

        env, calls = self.fake_claude(tmp_path)
        code, out, _dur, _timed = run_harness(
            self.rig(add_dirs=str(tmp_path / "gone")), "hi", tmp_path, env=env
        )
        assert code == 126 and "not a directory" in out
        assert list(calls.iterdir()) == []

    def test_an_older_binary_fails_the_turn_with_the_reason(self, tmp_path):
        from dispatch import run_harness

        env, calls = self.fake_claude(tmp_path, version="1.0.0")
        code, out, _dur, _timed = run_harness(
            self.rig(deny_tools="Bash(rm:*)"), "hi", tmp_path, env=env
        )
        assert code == 126 and "older than" in out
        assert list(calls.iterdir()) == []

    def test_a_tilde_entry_expands_against_the_rig_envs_home(self, tmp_path):
        from dispatch import run_harness

        home = tmp_path / "agent-home"
        (home / "lib").mkdir(parents=True)
        env, calls = self.fake_claude(tmp_path)
        run_harness(
            self.rig(add_dirs="~/lib", env={"HOME": str(home)}), "hi", tmp_path, env=env
        )
        [call] = list(calls.iterdir())
        argv = json.loads(call.read_text())
        assert argv[argv.index("--add-dir") + 1] == str(home / "lib")

    def test_a_turn_with_no_knob_never_probes_the_binary(self, tmp_path):
        from dispatch import run_harness

        env, calls = self.fake_claude(tmp_path, version="1.0.0")
        code, _out, _dur, _timed = run_harness(self.rig(), "hi", tmp_path, env=env)
        assert code == 0 and len(list(calls.iterdir())) == 1


# --- the trust check ------------------------------------------------------------------


def write_runbook(root: Path, text: str) -> None:
    import textwrap

    (root / runbook.RUNBOOK_NAME).write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


class TestTrustCheck:
    @pytest.fixture
    def node(self, tmp_path):
        root = tmp_path / "node"
        root.mkdir()
        return root

    @pytest.fixture
    def trusted(self, r4t_home):
        rig_mod.raise_machine_ceiling("trusted")
        return "trusted"

    def member_errors(self, node, name="Lead", **kwargs):
        book = runbook.load_for_root(node, validate=False, **kwargs)
        return "; ".join(book.roster.find(name).errors)

    def test_an_inline_add_dir_on_an_untrusted_node_fails_closed(self, node, r4t_home):
        write_runbook(node, """
            ## Roster

            ### Lead
            - **Engine:** claude --add-dir /tmp
            - **Leader:** yes
        """)
        assert "checked-in file" in self.member_errors(node)

    def test_an_inline_allow_tool_on_an_untrusted_node_fails_closed(self, node, r4t_home):
        write_runbook(node, """
            ## Roster

            ### Lead
            - **Engine:** copilot --allow-tool 'url(x.example)'
            - **Leader:** yes
        """)
        assert "allow_tools widens access" in self.member_errors(node)

    def test_a_deny_is_always_welcome(self, node, r4t_home):
        write_runbook(node, """
            ## Roster

            ### Lead
            - **Engine:** claude --deny-tool 'Bash(rm:*)'
            - **Leader:** yes
        """)
        assert self.member_errors(node) == ""

    def test_a_trusted_node_takes_the_widening(self, node, trusted):
        write_runbook(node, """
            ## Roster

            ### Lead
            - **Engine:** claude --add-dir /tmp --allow-tool Read
            - **Leader:** yes
        """)
        rig = runbook.load_for_root(node, node=trusted).roster.find("Lead").rig_override
        assert rig.add_dirs == ["/tmp"] and rig.allow_tools == ["Read"]

    def test_a_rig_block_is_checked_the_same_way(self, node, r4t_home):
        write_runbook(node, """
            ## Roster

            ### Lead
            - **Rig:** wide
            - **Leader:** yes

            ## Rigs

            ### wide
            - **Engine:** claude
            - **Add dirs:** /tmp
        """)
        book = runbook.load_for_root(node, validate=False)
        assert "add_dirs widens access" in book.rigs["wide"].error

    def test_the_trust_check_follows_the_extends_chain(self, node, r4t_home):
        write_runbook(node, """
            ---
            name: "base"
            ---

            ## Roster

            ### Lead
            - **Engine:** claude --add-dir /tmp
            - **Leader:** yes
        """)
        child = node / "child"
        child.mkdir()
        (child / runbook.RUNBOOK_NAME).write_text(
            '---\nname: "child"\nextends: ../r4t.md\n---\n\n## Mission\n\nShip it.\n',
            encoding="utf-8",
        )
        book = runbook.load_for_root(child, validate=False)
        assert "checked-in file" in "; ".join(book.roster.find("Lead").errors)

    def test_a_rig_block_may_not_say_it_twice(self, node, trusted):
        write_runbook(node, """
            ## Roster

            ### Lead
            - **Rig:** wide
            - **Leader:** yes

            ## Rigs

            ### wide
            - **Engine:** claude --add-dir /tmp
            - **Add dirs:** /var
        """)
        book = runbook.load_for_root(node, node=trusted, validate=False)
        assert "delete one" in book.rigs["wide"].error

    def test_the_rig_block_accepts_the_three_list_keys(self):
        assert {"adddirs", "allowtools", "denytools"} <= runbook.RIG_KEYS

    def test_a_rig_block_deny_is_welcome_on_an_untrusted_node(self, node, r4t_home):
        write_runbook(node, """
            ## Roster

            ### Lead
            - **Rig:** guarded
            - **Leader:** yes

            ## Rigs

            ### guarded
            - **Engine:** copilot
            - **Deny tools:** shell(git push), shell(rm)
        """)
        book = runbook.load_for_root(node, validate=False)
        assert book.rigs["guarded"].error is None
        assert book.rigs["guarded"].deny_tools == ["shell(git push)", "shell(rm)"]

    def test_the_engine_line_flags_repeat_and_each_value_parses_alone(self):
        spec = runbook.parse_engine_line(
            "copilot --deny-tool 'shell(a,b)' --deny-tool shell(c) --allow-tool url(x.example)"
        )
        assert spec["deny_tools"] == "shell(a,b),shell(c)"
        assert spec["allow_tools"] == "url(x.example)"
        with pytest.raises(runbook.RunbookError, match="unbalanced"):
            runbook.parse_engine_line("copilot --deny-tool 'shell(a' --deny-tool 'b)'")
        with pytest.raises(runbook.RunbookError, match="starts with '-'"):
            runbook.parse_engine_line("copilot --deny-tool --yolo")


# --- one resolver for the engine home -------------------------------------------------


class TestEngineHome:
    def test_a_non_empty_variable_wins(self, tmp_path):
        env = {"COPILOT_HOME": str(tmp_path / "x"), "HOME": str(tmp_path / "h")}
        assert engine_home("copilot", env) == tmp_path / "x"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_an_empty_variable_falls_back_to_the_childs_home(self, tmp_path, value):
        env = {"COPILOT_HOME": value, "HOME": str(tmp_path / "h")}
        assert engine_home("copilot", env) == tmp_path / "h" / ".copilot"

    def test_an_absent_variable_uses_the_childs_home_not_this_process(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "router"))
        assert engine_home("claude", {"HOME": str(tmp_path / "h")}) == tmp_path / "h" / ".claude"
        assert engine_home("codex", {"HOME": str(tmp_path / "h")}) == tmp_path / "h" / ".codex"

    def test_with_no_environment_given_this_process_is_asked(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "p"))
        assert engine_home("copilot") == tmp_path / "p"

    def test_a_pinned_session_is_found_in_the_spawned_home_not_the_routers(
        self, tmp_path, monkeypatch
    ):
        session = "11111111-2222-3333-4444-555555555555"
        spawned = tmp_path / "spawned"
        (spawned / "session-state" / session).mkdir(parents=True)
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "router"))
        argv = engine_run._build_argv_template(
            "copilot", model=None, timeout=900, workdir=tmp_path, session=session,
            env={"COPILOT_HOME": str(spawned)},
        )[0]
        assert f"--resume={session}" in argv and "--session-id" not in argv

    def test_the_same_session_is_founded_when_the_spawned_home_lacks_it(
        self, tmp_path, monkeypatch
    ):
        session = "11111111-2222-3333-4444-555555555555"
        router = tmp_path / "router"
        (router / "session-state" / session).mkdir(parents=True)
        monkeypatch.setenv("COPILOT_HOME", str(router))
        argv = engine_run._build_argv_template(
            "copilot", model=None, timeout=900, workdir=tmp_path, session=session,
            env={"COPILOT_HOME": str(tmp_path / "empty")},
        )[0]
        assert "--session-id" in argv and f"--resume={session}" not in argv

    def test_the_copilot_token_reads_the_spawned_homes_config(self, tmp_path, monkeypatch):
        from engines import copilot as copilot_engine

        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        home = tmp_path / "spawned"
        home.mkdir()
        (home / "config.json").write_text(
            '// User settings\n{"copilotTokens": "tok-spawned"}', encoding="utf-8"
        )
        assert copilot_engine.token({"COPILOT_HOME": str(home)}) == "tok-spawned"

    def test_the_transcript_probe_reads_the_spawned_claude_home(self, tmp_path, monkeypatch):
        from test_transcript import _claude_session

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "router-config"))
        spawned = tmp_path / "spawned-config"
        work = tmp_path / "repo"
        _claude_session(spawned, work, read=100, created=10, fresh=1)
        assert transcript.probe("claude", work) is None
        convo = transcript.probe("claude", work, {"CLAUDE_CONFIG_DIR": str(spawned)})
        assert convo is not None and convo.measured
