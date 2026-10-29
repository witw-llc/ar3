"""List filters retain the address book and local name precedence."""
import pytest

import commands
import listing
from registry import save_registry


@pytest.fixture
def address_book(fake_home, tmp_path, monkeypatch):
    save_registry({"Local": {"root": str(tmp_path)}})
    heard = {"local": "2026-10-25T12:00:00Z", "old": "2026-10-25T12:00:00Z",
             "edge": "2026-10-26T12:00:00Z", "new": "2026-10-27T12:00:00Z"}
    monkeypatch.setattr(commands, "last_heard", lambda: dict(heard))
    monkeypatch.setattr(listing.time, "time", lambda: 1793102400.0)
    return heard


@pytest.mark.parametrize(("args", "names"), [
    ([], ["Local", "edge", "new", "old"]),
    (["--local"], ["Local"]),
    (["--remote"], ["edge", "new", "old"]),
    (["--since", "2026-10-26T12:00:00Z"], ["Local", "edge", "new"]),
    (["--remote", "--since", "2026-10-26T12:00:00Z"], ["edge", "new"]),
    (["--local", "--since", "2026-10-28"], ["Local"]),
    (["--remote", "--since", "2026-10-28"], []),
])
def test_selection(address_book, capsys, args, names):
    before = dict(address_book)
    assert commands.cmd_ls(["-q", *args]) == 0
    assert capsys.readouterr().out.splitlines() == names
    assert address_book == before


def test_duration_matches_absolute_boundary(address_book, capsys, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(listing.time, "time", lambda: datetime(2026, 10, 27, 12, tzinfo=timezone.utc).timestamp())
    assert commands.cmd_ls(["--remote", "--since", "24h", "-q"]) == 0
    assert capsys.readouterr().out.splitlines() == ["edge", "new"]


@pytest.mark.parametrize("args", [
    ["--autostart", "--remote"], ["--autostart", "--since", "24h"],
    ["--local", "--remote"], ["--since"], ["--since", "nonsense"],
    ["--since", "nan"], ["--since", "inf"], ["--since", "-1h"], ["--bogus"], ["--loc"],
])
def test_bad_arguments_are_errors(fake_home, args, capsys):
    assert commands.cmd_ls(args) == 2
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("command", [commands.cmd_ls, commands.cmd_ps])
def test_help_needs_no_registry(monkeypatch, command, capsys):
    monkeypatch.setattr(commands, "load_registry", lambda: pytest.fail("help read state"))
    assert command(["--help"]) == 0
    assert "usage:" in capsys.readouterr().out


def test_ps_rejects_remote_status(fake_home, capsys):
    assert commands.cmd_ps(["--remote"]) == 2
    assert "error:" in capsys.readouterr().err


def test_empty_filtered_table_says_no_match(address_book, capsys):
    assert commands.cmd_ls(["--remote", "--since", "2026-10-28"]) == 0
    assert "no matching nodes" in capsys.readouterr().out


def test_startup_listing_uses_shared_parser(address_book, monkeypatch, capsys):
    monkeypatch.setattr(commands.autostart, "listing", lambda names: [
        (name, "off", "disarmed", "boot", "absent") for name in names
    ])
    assert commands.cmd_ls(["--autostart", "--local", "-q"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Local"]
    assert commands.cmd_ls(["--autostart", "--help"]) == 0
    assert "--autostart" in capsys.readouterr().out
