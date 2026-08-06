"""The CLI surface: what a reader of the guides sees, and what stays hidden."""
from __future__ import annotations

import re

import pytest

import r4t
from r4t import main as r4t_main

VISIBLE = ["init", "roster", "rig", "status", "logs", "chat", "seat", "flush", "task", "check"]


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
    for name in ("logs", "chat", "seat", "flush", "status", "task list", "check"):
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
