from __future__ import annotations

import cli


def test_rm_is_a_known_command():
    assert "rm" in cli.KNOWN_COMMANDS
    assert "rm <name>" in cli.CLI_EPILOG


def test_rm_dispatches_to_remove(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_remove", lambda args: calls.append(args) or 7)

    assert cli.dispatch("rm", ["alice"], interval=1.0) == 7
    assert calls == [["alice"]]


def test_remove_still_dispatches_to_same_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_remove", lambda args: calls.append(args) or 0)

    assert cli.dispatch("remove", ["alice"], interval=1.0) == 0
    assert calls == [["alice"]]


def test_defs_is_alias_for_definitions(monkeypatch):
    assert "defs" in cli.KNOWN_COMMANDS
    assert "definitions" in cli.KNOWN_COMMANDS
    calls = []
    monkeypatch.setattr(cli, "cmd_definitions", lambda args: calls.append(args) or 3)

    assert cli.dispatch("defs", ["ls"], interval=1.0) == 3
    assert cli.dispatch("definitions", ["add", "x.json"], interval=1.0) == 3
    assert calls == [["ls"], ["add", "x.json"]]


def test_vars_dispatches(monkeypatch):
    assert "vars" in cli.KNOWN_COMMANDS
    calls = []
    monkeypatch.setattr(cli, "cmd_vars", lambda args: calls.append(args) or 0)
    assert cli.dispatch("vars", ["bob", "set", "MODEL", "x"], interval=1.0) == 0
    assert calls == [["bob", "set", "MODEL", "x"]]


def test_restart_dispatches(monkeypatch):
    assert "restart" in cli.KNOWN_COMMANDS
    calls = []
    monkeypatch.setattr(cli, "cmd_restart", lambda args: calls.append(args) or 0)
    assert cli.dispatch("restart", ["qwen", "--force"], interval=1.0) == 0
    assert calls == [["qwen", "--force"]]


def test_update_dispatches(monkeypatch):
    assert "update" in cli.KNOWN_COMMANDS
    calls = []
    monkeypatch.setattr(cli, "cmd_update", lambda args: calls.append(args) or 0)
    assert cli.dispatch("update", ["--force"], interval=1.0) == 0
    assert calls == [["--force"]]


class TestUnreadableRegistry:
    """A registry that exists but does not parse is named as such. Reading it
    as empty made `ls` say nothing was registered and `convo` say the agent
    did not exist."""

    def _tear(self):
        from core import registry_path

        registry_path().parent.mkdir(parents=True, exist_ok=True)
        registry_path().write_text('{"agents": {"alice": {"root"')

    def test_ls_names_the_unreadable_registry(self, fake_home, capsys):
        self._tear()
        assert cli.main(["ls"]) == 1
        captured = capsys.readouterr()
        assert "registry unreadable" in captured.err
        assert "no nodes registered" not in captured.out

    def test_convo_does_not_claim_the_agent_is_missing(self, fake_home, capsys):
        self._tear()
        assert cli.main(["convo", "alice"]) == 1
        err = capsys.readouterr().err
        assert "registry unreadable" in err
        assert "no agent named" not in err
