"""node→root stamping — `--node` works from anywhere, CWD finds the node."""
from __future__ import annotations

import pytest

import state
from r4t import main as r4t_main

NODE = "acme"


def test_stamp_and_read_root(r4t_home, repo):
    assert state.read_root(NODE) is None
    state.stamp_root(NODE, repo)
    assert state.read_root(NODE) == repo
    state.stamp_root(NODE, repo)  # idempotent rewrite
    assert state.read_root(NODE) == repo


def test_node_for_root_matches_subdirs(r4t_home, repo, tmp_path):
    state.stamp_root(NODE, repo)
    state.stamp_root("other", tmp_path / "elsewhere")
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    assert state.node_for_root(sub) == NODE
    assert state.node_for_root(repo) == NODE
    assert state.node_for_root(tmp_path) is None


def test_dispatch_stamps_root(r4t_home, repo, rig_config, fake_harness):
    rc = r4t_main([
        "dispatch",
        "--root", str(repo),
        "--from", "gerry",
        "--to", f"{NODE}:phil",
        "--message", "stamp me",
        "--rig-config", str(rig_config),
        "--no-notify",
    ])
    assert rc == 0
    assert state.read_root(NODE) == repo


def test_status_from_unrelated_cwd_uses_stamped_root(
    r4t_home, repo, rig_config, tmp_path, monkeypatch, capsys
):
    state.stamp_root(NODE, repo)
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    rc = r4t_main(["status", "--node", NODE, "--rig-config", str(rig_config)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "roster not found" not in out
    assert "Health" in out
    assert "Gerry" in out


def test_bare_node_resolves_from_cwd(
    r4t_home, repo, rig_config, monkeypatch, capsys
):
    state.stamp_root(NODE, repo)
    state.team_dir("other").mkdir(parents=True)  # two teams: ambiguity is real
    monkeypatch.chdir(repo)
    rc = r4t_main(["status", "--rig-config", str(rig_config)])
    assert rc == 0
    assert "team: acme" in capsys.readouterr().out


def test_bare_node_still_errors_outside_any_root(
    r4t_home, repo, tmp_path, monkeypatch, capsys
):
    state.team_dir(NODE).mkdir(parents=True)
    state.team_dir("other").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    rc = r4t_main(["status"])
    assert rc == 2
    assert "pass --node" in capsys.readouterr().err


# ---------- portable orgs: the workplace repo also infers the node ----------

def _portable(tmp_path, node, org_name, workplace):
    import json

    from org import ORG_CONFIG_NAME

    org_dir = tmp_path / org_name
    org_dir.mkdir()
    (org_dir / ORG_CONFIG_NAME).write_text(
        json.dumps({"repo": str(workplace)}), encoding="utf-8"
    )
    state.stamp_root(node, org_dir)
    return org_dir


def test_workplace_cwd_infers_the_node(r4t_home, tmp_path):
    workplace = tmp_path / "novel-repo"
    workplace.mkdir()
    _portable(tmp_path, NODE, "org", workplace)
    state.stamp_root("other", tmp_path / "elsewhere")
    sub = workplace / "chapters"
    sub.mkdir()
    assert state.node_for_root(workplace) == NODE
    assert state.node_for_root(sub) == NODE


def test_org_dir_cwd_beats_workplace_lookup(r4t_home, tmp_path):
    workplace = tmp_path / "novel-repo"
    workplace.mkdir()
    org_dir = _portable(tmp_path, NODE, "org", workplace)
    assert state.node_for_root(org_dir) == NODE


def test_shared_workplace_stays_ambiguous(r4t_home, tmp_path):
    # The A/B case: two org dirs, one repo. Standing in the repo cannot pick
    # a side, so inference declines and the CLI still asks for --node.
    workplace = tmp_path / "shared-repo"
    workplace.mkdir()
    _portable(tmp_path, "org-a-node", "org-a", workplace)
    _portable(tmp_path, "org-b-node", "org-b", workplace)
    assert state.node_for_root(workplace) is None


def test_bare_status_resolves_from_workplace_cwd(
    r4t_home, tmp_path, monkeypatch, capsys
):
    workplace = tmp_path / "novel-repo"
    workplace.mkdir()
    org_dir = _portable(tmp_path, NODE, "org", workplace)
    (org_dir / "ROSTER.md").write_text(
        "# Roster\n\n### Gerry\n- **Rig:** leader\n"
        "- **Leader:** yes\n",
        encoding="utf-8",
    )
    state.team_dir("other").mkdir(parents=True)  # two teams: ambiguity is real
    monkeypatch.chdir(workplace)
    rc = r4t_main(["status"])
    assert rc == 0
    assert f"team: {NODE}" in capsys.readouterr().out


# ---------- ambiguity is an error, not a result: every command exits non-zero ----------

AMBIGUOUS_ARGVS = [
    ["status"],
    ["seat"],
    ["seat", "send", "hello"],
    ["seat", "inbox"],
    ["chat", "--plain"],
    ["logs"],
    ["task", "list"],
    ["clear"],
    ["idle"],
]


@pytest.mark.parametrize("argv", AMBIGUOUS_ARGVS, ids=lambda a: " ".join(a))
def test_ambiguous_team_exits_nonzero(r4t_home, tmp_path, monkeypatch, capsys, argv):
    # The live hour-long stall: a scripted `r4t seat send` printed the
    # pass-node hint but the pipeline read exit 0 (the pipe's last command
    # masked it). r4t's side of the contract is a hard non-zero exit from
    # EVERY command that resolves a node, so a plain (unpiped) invocation
    # can never mask a no-op as success.
    state.team_dir("aaa").mkdir(parents=True)
    state.team_dir("bbb").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)  # no stamped root matches cwd
    rc = r4t_main([*argv, "--simulate-tell"] if argv[0] in ("seat", "chat") else argv)
    assert rc == 2
    assert "pass --node" in capsys.readouterr().err


@pytest.mark.parametrize("argv", AMBIGUOUS_ARGVS, ids=lambda a: " ".join(a))
def test_no_teams_exits_nonzero(r4t_home, tmp_path, monkeypatch, capsys, argv):
    monkeypatch.chdir(tmp_path)
    rc = r4t_main([*argv, "--simulate-tell"] if argv[0] in ("seat", "chat") else argv)
    assert rc == 2
    assert "pass --node" in capsys.readouterr().err
