"""Portable orgs — ROSTER.md/MISSION.md outside the repo, resolution + graduation."""
from __future__ import annotations

import json
import sys

import state
from org import COMMS_CLOSED, ORG_CONFIG_NAME, check_org, load_org
from r4t import main as r4t_main

NODE = "acme"

# Self-contained roster/config (no `from conftest import` — running the a8s and
# r4t suites together makes the bare `conftest` module ambiguous at collection).
CLEAN_ROSTER = """\
# Roster

### Gerry
- **Rig:** leader
- **Role:** Lead
- **Leader:** yes

### Phil
- **Rig:** junior-dev
- **Role:** Developer
"""

ROSTER_TEXT = CLEAN_ROSTER


def _prompt_of(fake_harness) -> str:
    _script, out = fake_harness
    calls = sorted(out.iterdir())
    assert calls, "the harness never ran"
    return calls[-1].read_text(encoding="utf-8")


def _rig_config(tmp_path, fake_harness):
    script, _out = fake_harness
    invoke = [sys.executable, str(script), "{prompt}"]
    config = {
        "throttle": {"max_concurrent": 0, "min_seconds_between_turn_starts": 0},
        "cell_budget_max": 200,
        "cell_budget_earn_per_hour": 100,
        "leader": {"invoke": invoke, "timeout_seconds": 30, "budget_max": 100},
        "junior-dev": {"invoke": invoke, "timeout_seconds": 30, "budget_max": 100},
        "pins": {"gerry": "leader"},
    }
    path = tmp_path / "rigs.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


# ---------- unit: resolution + precedence ----------

def test_in_repo_is_the_default(tmp_path):
    org = load_org(tmp_path)
    assert org.dir == tmp_path and org.workplace == tmp_path
    assert not org.is_portable


def test_org_config_points_at_a_workplace(tmp_path):
    org_dir = tmp_path / "org"
    org_dir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (org_dir / ORG_CONFIG_NAME).write_text(json.dumps({"repo": str(repo)}), encoding="utf-8")
    org = load_org(org_dir)
    assert org.dir == org_dir and org.workplace == repo
    assert org.is_portable


def test_relative_repo_resolves_against_the_org_dir(tmp_path):
    org_dir = tmp_path / "org"
    (org_dir).mkdir()
    (tmp_path / "repo").mkdir()
    (org_dir / ORG_CONFIG_NAME).write_text(json.dumps({"repo": "../repo"}), encoding="utf-8")
    assert load_org(org_dir).workplace == (tmp_path / "repo").resolve()


def test_malformed_config_degrades_but_check_reports(tmp_path):
    (tmp_path / ORG_CONFIG_NAME).write_text("{ not json", encoding="utf-8")
    org = load_org(tmp_path)  # never raises — degrades to in-repo
    assert org.workplace == tmp_path
    assert any("cannot read org config" in m for m in check_org(tmp_path))


def test_repo_less_config_is_valid_in_repo_default(tmp_path):
    # A config WITHOUT a repo key is legal — it exists purely to carry org
    # settings; resolution falls back to the in-repo default.
    (tmp_path / ORG_CONFIG_NAME).write_text(
        json.dumps({"comms": "closed", "egress": False}), encoding="utf-8"
    )
    org = load_org(tmp_path)
    assert not org.is_portable and org.workplace == tmp_path
    assert org.comms == COMMS_CLOSED and org.egress is False
    assert check_org(tmp_path) == []


def test_check_reports_absent_workplace(tmp_path):
    (tmp_path / ORG_CONFIG_NAME).write_text(
        json.dumps({"repo": str(tmp_path / "nope")}), encoding="utf-8"
    )
    assert any("does not exist" in m for m in check_org(tmp_path))


def test_settings_parse_with_defaults(tmp_path):
    org = load_org(tmp_path)  # no config at all
    assert org.comms == "open" and org.leader_sees_lateral is False and org.egress is True
    assert org.priority_senders == []


def test_shipped_priority_senders_default_is_empty(tmp_path):
    # The default used to be `["neil*"]` — the owner's own name, reaching the
    # public mirror as shipped policy. No name ships by default; an org that
    # wants a Tier-1 sender states one explicitly (see the AR3 org's own
    # r4t.md frontmatter).
    from schedule import DEFAULT_PRIORITY_SENDERS

    assert DEFAULT_PRIORITY_SENDERS == ()
    assert load_org(tmp_path).priority_senders == []


def test_priority_senders_are_an_org_setting(tmp_path):
    # Who the org answers to first is a property of the org, not of the
    # machine or the rig, so it travels with ROSTER.md and MISSION.md.
    (tmp_path / ORG_CONFIG_NAME).write_text(
        json.dumps({"priority_senders": ["ada*", " grace@* "]}), encoding="utf-8"
    )
    org = load_org(tmp_path)
    assert org.priority_senders == ["ada*", "grace@*"]
    assert check_org(tmp_path) == []


def test_an_empty_priority_list_leaves_a_pure_score_rotation(tmp_path):
    (tmp_path / ORG_CONFIG_NAME).write_text(
        json.dumps({"priority_senders": []}), encoding="utf-8"
    )
    assert load_org(tmp_path).priority_senders == []


def test_check_flags_a_malformed_priority_list(tmp_path):
    (tmp_path / ORG_CONFIG_NAME).write_text(
        json.dumps({"priority_senders": "neil*"}), encoding="utf-8"
    )
    assert any('"priority_senders" must be a list' in m for m in check_org(tmp_path))


def test_check_flags_bad_setting_values(tmp_path):
    (tmp_path / ORG_CONFIG_NAME).write_text(
        json.dumps({"comms": "loud", "leader_sees_lateral": "yes"}), encoding="utf-8"
    )
    problems = check_org(tmp_path)
    assert any('"comms" must be "open" or "closed"' in m for m in problems)
    assert any('"leader_sees_lateral" must be true or false' in m for m in problems)
    # load_org still degrades to safe defaults rather than raising
    org = load_org(tmp_path)
    assert org.comms == "open" and org.leader_sees_lateral is False
    # ...and no longer discards the same findings — a caller that dispatches
    # on this org can still say so, instead of the operator finding out only
    # by running `roster check`.
    assert any('"comms" must be "open" or "closed"' in m for m in org.errors)
    assert any('"leader_sees_lateral" must be true or false' in m for m in org.errors)


def test_a_clean_config_carries_no_errors(tmp_path):
    (tmp_path / ORG_CONFIG_NAME).write_text(json.dumps({"comms": "closed"}), encoding="utf-8")
    assert load_org(tmp_path).errors == []


def test_dispatch_warns_on_a_malformed_org_setting_but_still_runs(
    r4t_home, tmp_path, fake_harness, capsys
):
    # A malformed `comms:`/`egress:` used to vanish silently the moment
    # `load_org` degraded to defaults — invisible until the operator happened
    # to run `roster check`. Dispatch now says so, and still runs.
    root = tmp_path / "solo"
    root.mkdir()
    (root / "ROSTER.md").write_text(CLEAN_ROSTER, encoding="utf-8")
    (root / ORG_CONFIG_NAME).write_text(json.dumps({"comms": "loud"}), encoding="utf-8")
    cfg = _rig_config(tmp_path, fake_harness)
    rc = r4t_main([
        "dispatch", "--root", str(root),
        "--from", "boss", "--to", f"{NODE}:gerry", "--message", "go",
        "--rig-config", str(cfg), "--no-notify",
    ])
    err = capsys.readouterr().err
    assert rc == 0  # loud, not fatal — the turn still runs on the default
    assert 'warning: org config "comms" must be "open" or "closed"' in err


def _portable_org(tmp_path, mission="Ship the thing and stop."):
    org_dir = tmp_path / "org"
    org_dir.mkdir()
    workplace = tmp_path / "workplace"
    workplace.mkdir()
    (org_dir / "ROSTER.md").write_text(ROSTER_TEXT, encoding="utf-8")
    (org_dir / ORG_CONFIG_NAME).write_text(json.dumps({"repo": str(workplace)}), encoding="utf-8")
    if mission is not None:
        (org_dir / "MISSION.md").write_text(mission, encoding="utf-8")
    return org_dir, workplace


def test_dispatch_reads_org_docs_but_works_in_the_repo(r4t_home, tmp_path, fake_harness):
    org_dir, workplace = _portable_org(tmp_path)
    cfg = _rig_config(tmp_path, fake_harness)
    rc = r4t_main([
        "dispatch", "--root", str(org_dir),
        "--from", "boss", "--to", f"{NODE}:gerry", "--message", "go",
        "--rig-config", str(cfg), "--no-notify",
    ])
    assert rc == 0
    prompt = _prompt_of(fake_harness)
    assert f"working directory is {workplace.resolve()}" in prompt  # turns run in the repo
    assert "Ship the thing and stop." in prompt              # MISSION read from org dir
    assert state.read_root(NODE) == org_dir                  # node stamped to org dir


def test_graduation_falls_back_to_in_repo(r4t_home, tmp_path, fake_harness):
    # Graduation: copy the docs into the repo and drop the pointer.
    _org_dir, workplace = _portable_org(tmp_path)
    (workplace / "ROSTER.md").write_text(ROSTER_TEXT, encoding="utf-8")
    (workplace / "MISSION.md").write_text("Ship the thing and stop.", encoding="utf-8")
    cfg = _rig_config(tmp_path, fake_harness)
    rc = r4t_main([
        "dispatch", "--root", str(workplace),
        "--from", "boss", "--to", f"{NODE}:gerry", "--message", "go",
        "--rig-config", str(cfg), "--no-notify",
    ])
    assert rc == 0
    prompt = _prompt_of(fake_harness)
    assert f"working directory is {workplace.resolve()}" in prompt
    assert "Ship the thing and stop." in prompt
    assert load_org(workplace).workplace == workplace  # no pointer -> in-repo default


def test_roster_check_runs_against_an_org_dir(r4t_home, tmp_path, fake_harness, capsys):
    org_dir, _workplace = _portable_org(tmp_path)
    (org_dir / "ROSTER.md").write_text(CLEAN_ROSTER, encoding="utf-8")
    cfg = _rig_config(tmp_path, fake_harness)
    rc = r4t_main(["roster", "check", "--root", str(org_dir), "--rig-config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK" in out and "leader Gerry" in out


def test_roster_check_flags_a_bad_org_config(r4t_home, tmp_path, fake_harness, capsys):
    org_dir, _workplace = _portable_org(tmp_path)
    (org_dir / "ROSTER.md").write_text(CLEAN_ROSTER, encoding="utf-8")
    (org_dir / ORG_CONFIG_NAME).write_text(
        json.dumps({"repo": str(tmp_path / "gone")}), encoding="utf-8"
    )
    cfg = _rig_config(tmp_path, fake_harness)
    rc = r4t_main(["roster", "check", "--root", str(org_dir), "--rig-config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "org:" in out and "does not exist" in out


KNOWLEDGE_ROSTER = """\
# Roster

### Gerry
- **Rig:** leader
- **Role:** Lead
- **Leader:** yes

### Phil
- **Rig:** junior-dev
- **Role:** Developer
- **Knowledge:** on
"""


def _rig_config_with_preset(tmp_path, fake_harness, preset):
    script, _out = fake_harness
    invoke = [sys.executable, str(script), "{prompt}"]
    config = {
        "throttle": {"max_concurrent": 0, "min_seconds_between_turn_starts": 0},
        "cell_budget_max": 200,
        "cell_budget_earn_per_hour": 100,
        "leader": {"invoke": invoke, "timeout_seconds": 30, "budget_max": 100},
        "junior-dev": {
            "invoke": invoke, "timeout_seconds": 30, "budget_max": 100, "preset": preset,
        },
        "pins": {"gerry": "leader"},
    }
    path = tmp_path / "rigs.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_roster_check_warns_on_a_below_floor_knowledge_rig(r4t_home, tmp_path, fake_harness, capsys):
    org_dir, _workplace = _portable_org(tmp_path, mission=None)
    (org_dir / "ROSTER.md").write_text(KNOWLEDGE_ROSTER, encoding="utf-8")
    cfg = _rig_config_with_preset(tmp_path, fake_harness, "ollama")
    rc = r4t_main(["roster", "check", "--root", str(org_dir), "--rig-config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "warning: Phil: Knowledge is on with rig 'junior-dev'" in out
    assert "bytes, not tokens" in out


def test_roster_check_does_not_warn_above_the_floor(r4t_home, tmp_path, fake_harness, capsys):
    org_dir, _workplace = _portable_org(tmp_path, mission=None)
    (org_dir / "ROSTER.md").write_text(KNOWLEDGE_ROSTER, encoding="utf-8")
    cfg = _rig_config_with_preset(tmp_path, fake_harness, "claude")
    rc = r4t_main(["roster", "check", "--root", str(org_dir), "--rig-config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Knowledge" not in out


def test_roster_check_flags_an_unresolvable_distill_rig(r4t_home, tmp_path, fake_harness, capsys):
    roster_text = KNOWLEDGE_ROSTER.replace("- **Knowledge:** on", "- **Knowledge:** ghost")
    org_dir, _workplace = _portable_org(tmp_path, mission=None)
    (org_dir / "ROSTER.md").write_text(roster_text, encoding="utf-8")
    cfg = _rig_config_with_preset(tmp_path, fake_harness, "claude")
    rc = r4t_main(["roster", "check", "--root", str(org_dir), "--rig-config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Phil: Knowledge distill rig 'ghost' not found" in out


def test_two_orgs_one_repo_do_not_collide(r4t_home, tmp_path, fake_harness):
    # The A/B case: two org dirs (same repo) run as two a8s nodes; roster state is
    # per-node, so nothing collides.
    workplace = tmp_path / "shared-repo"
    workplace.mkdir()
    cfg = _rig_config(tmp_path, fake_harness)
    for org_name, node in (("org-a", "acme"), ("org-b", "beta")):
        org_dir = tmp_path / org_name
        org_dir.mkdir()
        (org_dir / "ROSTER.md").write_text(ROSTER_TEXT, encoding="utf-8")
        (org_dir / ORG_CONFIG_NAME).write_text(
            json.dumps({"repo": str(workplace)}), encoding="utf-8"
        )
        rc = r4t_main([
            "dispatch", "--root", str(org_dir),
            "--from", "boss", "--to", f"{node}:gerry", "--message", "go",
            "--rig-config", str(cfg), "--no-notify",
        ])
        assert rc == 0

    assert state.read_root("acme") == tmp_path / "org-a"
    assert state.read_root("beta") == tmp_path / "org-b"
    assert state.roster_dir("acme") != state.roster_dir("beta")
    assert (state.agent_dir("acme", "gerry")).is_dir()
    assert (state.agent_dir("beta", "gerry")).is_dir()


# ---------- observer surfaces resolve the stamped org dir like dispatch ----------

SHADOW_ROSTER = """\
# Shadow

### Impostor
- **Rig:** leader
- **Leader:** yes
"""


def _stamped_org(r4t_home, tmp_path):
    org_dir, workplace = _portable_org(tmp_path)
    state.stamp_root(NODE, org_dir)
    return org_dir, workplace


def test_status_reads_the_org_dir_not_the_cwd(
    r4t_home, tmp_path, fake_harness, monkeypatch, capsys
):
    _org_dir, workplace = _stamped_org(r4t_home, tmp_path)
    cfg = _rig_config(tmp_path, fake_harness)
    monkeypatch.chdir(workplace)
    rc = r4t_main(["status", "--node", NODE, "--rig-config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "roster not found" not in out
    assert "Gerry" in out


def test_status_ignores_a_shadow_roster_in_the_workplace(
    r4t_home, tmp_path, fake_harness, monkeypatch, capsys
):
    # A member wrote its own ROSTER.md into the workplace (it happened live
    # with MISSION.md). The stamped org dir stays authoritative.
    _org_dir, workplace = _stamped_org(r4t_home, tmp_path)
    (workplace / "ROSTER.md").write_text(SHADOW_ROSTER, encoding="utf-8")
    cfg = _rig_config(tmp_path, fake_harness)
    monkeypatch.chdir(workplace)
    rc = r4t_main(["status", "--node", NODE, "--rig-config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Gerry" in out
    assert "Impostor" not in out


def test_explicit_root_still_overrides_the_stamp(
    r4t_home, tmp_path, fake_harness, monkeypatch, capsys
):
    _org_dir, workplace = _stamped_org(r4t_home, tmp_path)
    (workplace / "ROSTER.md").write_text(SHADOW_ROSTER, encoding="utf-8")
    cfg = _rig_config(tmp_path, fake_harness)
    rc = r4t_main([
        "status", "--node", NODE, "--root", str(workplace), "--rig-config", str(cfg),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Impostor" in out


def test_tell_resolves_the_stamped_org_dir(
    r4t_home, tmp_path, fake_harness, monkeypatch, capsys
):
    _org_dir, workplace = _stamped_org(r4t_home, tmp_path)
    cfg = _rig_config(tmp_path, fake_harness)
    monkeypatch.chdir(workplace)
    rc = r4t_main([
        "tell", "--as", "gerry", "--to", "phil", "hi",
        "--node", NODE, "--rig-config", str(cfg), "--simulate-tell",
    ])
    assert rc == 0
    assert _prompt_of(fake_harness)


def test_logs_runs_against_an_org_dir_node(
    r4t_home, tmp_path, fake_harness, monkeypatch, capsys
):
    _org_dir, workplace = _stamped_org(r4t_home, tmp_path)
    state.append_log(NODE, "r4t: QUEUED boss -> gerry thread=T hop=0 \"go\" (depth 1)")
    monkeypatch.chdir(workplace)
    rc = r4t_main(["logs", "--node", NODE])
    out = capsys.readouterr().out
    assert rc == 0
    assert "QUEUED boss -> gerry" in out


def test_tell_adopts_the_root_when_no_stamp_exists(
    r4t_home, tmp_path, fake_harness, monkeypatch, capsys
):
    # The live quill sequence: a roster driven entirely through `tell` never
    # passes cmd_dispatch, so no stamp exists and observer commands guess
    # from cwd. One tell with --root writes the stamp; from then on the
    # workplace cwd resolves the node and the org dir.
    org_dir, workplace = _portable_org(tmp_path)
    cfg = _rig_config(tmp_path, fake_harness)
    assert state.read_root(NODE) is None

    rc = r4t_main([
        "tell", "--as", "gerry", "hi", "--node", NODE, "--root", str(org_dir),
        "--rig-config", str(cfg), "--simulate-tell",
    ])
    assert rc == 0
    assert state.read_root(NODE) == org_dir

    state.roster_dir("other").mkdir(parents=True)  # ambiguity is real
    monkeypatch.chdir(workplace)
    capsys.readouterr()
    rc = r4t_main(["status", "--rig-config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"roster: {NODE}" in out and "Gerry" in out


def test_tell_never_overrides_an_existing_stamp(
    r4t_home, tmp_path, fake_harness, capsys
):
    org_dir, workplace = _portable_org(tmp_path)
    state.stamp_root(NODE, org_dir)
    (workplace / "ROSTER.md").write_text(SHADOW_ROSTER, encoding="utf-8")
    cfg = _rig_config(tmp_path, fake_harness)
    rc = r4t_main([
        "tell", "--as", "gerry", "hi", "--node", NODE, "--root", str(workplace),
        "--rig-config", str(cfg), "--simulate-tell",
    ])
    assert rc == 2  # the shadow roster has no Gerry — tell refuses
    assert state.read_root(NODE) == org_dir  # and the stamp is untouched
