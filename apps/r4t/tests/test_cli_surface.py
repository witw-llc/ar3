"""The CLI surface: what a reader of the guides sees, and what stays hidden."""
from __future__ import annotations

import re

import pytest

import r4t
from r4t import main as r4t_main

VISIBLE = [
    "init", "roster", "rig", "status", "logs", "tell", "flush", "resume", "check",
]


def _top_level_help() -> str:
    return r4t.build_parser().format_help()


def _panel_rows() -> list[r4t.Command]:
    return [cmd for _section, cmds in r4t.COMMAND_HELP for cmd in cmds]


def _argv_for(display: str) -> list[str]:
    """Command path from a panel label, dropping <placeholder> arguments."""
    tokens = []
    for token in display.split():
        if token.startswith("<"):
            break
        tokens.append(token)
    return tokens


def test_help_hides_machinery_and_maintainer_verbs():
    text = _top_level_help()
    for name in r4t.HIDDEN_COMMANDS:
        assert not re.search(rf"\b{name}\b", text), f"{name} leaked into --help"


def test_help_usage_line_does_not_enumerate_commands():
    usage = _top_level_help().splitlines()[0]
    assert usage == "usage: r4t [-h] [--version] COMMAND ..."


def test_help_lists_every_user_command():
    text = _top_level_help()
    for name in VISIBLE:
        assert re.search(rf"^    {name}\b", text, re.MULTILINE), f"{name} missing from --help"


@pytest.mark.parametrize("name", r4t.HIDDEN_COMMANDS)
def test_hidden_commands_still_have_their_own_help(name):
    with pytest.raises(SystemExit) as exc:
        r4t_main([name, "--help"])
    assert exc.value.code == 0


def test_hidden_commands_still_parse():
    args = r4t.build_parser().parse_args(
        ["dispatch", "--from", "gerry", "--to", "acme:phil", "--message", "hi"]
    )
    assert args.func is r4t.cmd_dispatch
    assert r4t.build_parser().parse_args(["sandbox", "--fake"]).fake is True
    assert r4t.build_parser().parse_args(["idle"]).func is r4t.cmd_idle
    assert r4t.build_parser().parse_args(["clear"]).func is r4t.cmd_clear
    assert r4t.build_parser().parse_args(["lab", "list"]).func is r4t.cmd_lab_list
    assert r4t.build_parser().parse_args(["judge", "--rig", "j"]).func is r4t.cmd_judge


def test_panel_is_sectioned(capsys):
    r4t._print_command_panel()
    out = capsys.readouterr().out
    assert "Getting started" in out
    assert "Every day" in out
    assert "Verification" in out
    for name in ("logs", "tell", "flush", "status", "check"):
        assert name in out


def test_panel_hides_the_same_commands_as_help():
    text = "\n".join(cmd.display for cmd in _panel_rows())
    for name in r4t.HIDDEN_COMMANDS:
        assert not re.search(rf"\b{name}\b", text), f"{name} leaked into the panel"


def test_overview_renders_the_panel(r4t_home, repo, capsys, monkeypatch):
    monkeypatch.chdir(repo)
    assert r4t_main([]) == 0
    out = capsys.readouterr().out
    assert "Getting started" in out
    assert "Every day" in out
    assert "Next steps" in out
    for name in r4t.HIDDEN_COMMANDS:
        assert not re.search(rf"\b{name}\b", out), f"{name} leaked into the overview"


@pytest.mark.parametrize("display", [cmd.display for cmd in _panel_rows()])
def test_every_panel_command_parses(display):
    with pytest.raises(SystemExit) as exc:
        r4t_main([*_argv_for(display), "--help"])
    assert exc.value.code == 0


def test_panel_owns_the_subcommand_help_strings():
    text = _top_level_help()
    for cmd in _panel_rows():
        if cmd.parser:
            assert cmd.blurb in text, f"{cmd.parser} help drifted from the table"


def test_try_hints_point_at_visible_commands():
    for cmd in _panel_rows():
        if not cmd.hint:
            continue
        with pytest.raises(SystemExit) as exc:
            r4t_main([*_argv_for(cmd.hint.removeprefix("r4t ")), "--help"])
        assert exc.value.code == 0


class TestStrayPositionalAdoption:
    """argparse before 3.12 abandons a positional that trails optionals when
    earlier optional positionals matched empty; `_adopt_stray_positionals`
    picks those up so `engine <id> run --flags PROMPT` parses on every
    interpreter the suite deploys to."""

    def _engine_ns(self, argv: list[str]):
        args, extras = r4t.build_parser().parse_known_args(argv)
        return args, extras

    def _fabricated(self, **attrs):
        import argparse

        return argparse.Namespace(**attrs)

    def test_prompt_after_flags_is_adopted(self):
        ns = self._fabricated(action="run", prompt=None)
        assert r4t._adopt_stray_positionals(ns, ["Test"]) == []
        assert ns.prompt == "Test"

    def test_stdin_dash_is_adopted_as_prompt(self):
        ns = self._fabricated(action="run", prompt=None)
        assert r4t._adopt_stray_positionals(ns, ["-"]) == []
        assert ns.prompt == "-"

    def test_action_then_prompt_adopted_in_order(self):
        ns = self._fabricated(action=None, prompt=None)
        assert r4t._adopt_stray_positionals(ns, ["run", "hello"]) == []
        assert ns.action == "run"
        assert ns.prompt == "hello"

    def test_rig_run_namespace_adopts_prompt_without_action(self):
        ns = self._fabricated(rig="ar3-lead", prompt=None)
        assert r4t._adopt_stray_positionals(ns, ["hello"]) == []
        assert ns.prompt == "hello"

    def test_quota_action_never_gains_a_prompt(self):
        ns = self._fabricated(action="quota", prompt=None)
        assert r4t._adopt_stray_positionals(ns, ["Test"]) == ["Test"]

    def test_tell_message_keeps_taking_words(self):
        ns = self._fabricated(message=[])
        assert r4t._adopt_stray_positionals(ns, ["hello", "there"]) == []
        assert ns.message == ["hello", "there"]

    def test_rig_get_key_after_flags_is_adopted(self):
        ns = self._fabricated(rig="cheap", key=None)
        assert r4t._adopt_stray_positionals(ns, ["timeout"]) == []
        assert ns.key == "timeout"

    @pytest.mark.parametrize(
        "argv,dest,expected",
        [
            (["tell", "--as", "bob", "hi", "there"], "message", ["hi", "there"]),
            (["rig", "get", "cheap", "--rig-config", "x", "timeout"], "key", "timeout"),
            (["flush", "--node", "n1", "amos", "bo"], "members", ["amos", "bo"]),
        ],
    )
    def test_current_interpreter_parses_flags_before_the_positional(
        self, argv, dest, expected
    ):
        """The same 3.10 shape aec755e fixed for engine/rig-run, verified for
        every other parser with an optional positional that can trail flags.
        `flush` rides along because its `members` is the only positional in
        its parser, which 3.10 already places correctly."""
        args, extras = self._engine_ns(argv)
        if extras:
            extras = r4t._adopt_stray_positionals(args, extras)
        assert extras == []
        assert getattr(args, dest) == expected

    def test_flag_lookalikes_and_filled_namespaces_stay_unrecognized(self):
        ns = self._fabricated(action="run", prompt="already")
        assert r4t._adopt_stray_positionals(ns, ["--nope", "extra"]) == [
            "--nope",
            "extra",
        ]

    def test_namespace_without_prompt_adopts_nothing(self):
        ns = self._fabricated(rigs=["a"])
        assert r4t._adopt_stray_positionals(ns, ["stray"]) == ["stray"]

    def test_current_interpreter_parses_flags_before_prompt(self):
        args, extras = self._engine_ns(
            ["engine", "codex", "run", "--agent", "amos", "Test"]
        )
        if extras:
            extras = r4t._adopt_stray_positionals(args, extras)
        assert extras == []
        assert args.target == "codex"
        assert args.action == "run"
        assert args.prompt == "Test"
