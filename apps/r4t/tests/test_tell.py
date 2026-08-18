"""r4t tell --as — the owner's impersonation verb: jumpstart or diagnose."""
from __future__ import annotations

from r4t import main as r4t_main

NODE = "acme"


def _tell(repo, rig_config, *args):
    return r4t_main(
        [
            "tell",
            *args,
            "--node",
            NODE,
            "--root",
            str(repo),
            "--rig-config",
            str(rig_config),
            "--simulate-tell",
        ]
    )


def test_tell_as_lands_in_the_named_member_queue(repo, rig_config, r4t_home, fake_harness):
    assert _tell(repo, rig_config, "--as", "gerry", "--to", "phil", "ship", "it") == 0
    _script, out = fake_harness
    calls = sorted(out.iterdir())
    assert len(calls) == 1
    assert "Gerry" in calls[0].read_text(encoding="utf-8")


def test_tell_as_defaults_to_the_leader(repo, rig_config, r4t_home, fake_harness):
    assert _tell(repo, rig_config, "--as", "phil", "hello") == 0
    _script, out = fake_harness
    calls = sorted(out.iterdir())
    assert len(calls) == 1
    assert "You are Gerry" in calls[0].read_text(encoding="utf-8")


def test_tell_emits_a_queued_ticker_line(repo, rig_config, r4t_home, fake_harness, capsys):
    assert _tell(repo, rig_config, "--as", "gerry", "--to", "phil", "go") == 0
    out = capsys.readouterr().out
    assert "r4t: QUEUED phil from acme:gerry" in out


def test_tell_rejects_unknown_as_member(repo, rig_config, r4t_home, capsys):
    assert _tell(repo, rig_config, "--as", "nobody", "hi") == 2
    err = capsys.readouterr().err
    assert "no roster member named 'nobody'" in err


def test_tell_rejects_unknown_to_member(repo, rig_config, r4t_home, capsys):
    assert _tell(repo, rig_config, "--as", "gerry", "--to", "nobody", "hi") == 2
    assert "no dispatchable member" in capsys.readouterr().err


def test_tell_requires_a_message(repo, rig_config, r4t_home, capsys):
    assert _tell(repo, rig_config, "--as", "gerry") == 2
    assert "message is required" in capsys.readouterr().err
