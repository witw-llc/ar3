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
# Team Roster

### Neil
- **Human:** yes
- **Address:** neil
- **Role:** Director

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


def test_doorbell_check_string_accepted(tmp_path):
    (tmp_path / ORG_CONFIG_NAME).write_text(
        json.dumps({"doorbell_check": "r4t check acme"}), encoding="utf-8"
    )
    assert load_org(tmp_path).doorbell_check == "r4t check acme"
    assert check_org(tmp_path) == []


def test_doorbell_check_absent_by_default(tmp_path):
    assert load_org(tmp_path).doorbell_check is None


def test_doorbell_check_non_string_degrades_with_error(tmp_path):
    (tmp_path / ORG_CONFIG_NAME).write_text(
        json.dumps({"doorbell_check": ["r4t", "check"]}), encoding="utf-8"
    )
    assert any('"doorbell_check" must be a string' in m for m in check_org(tmp_path))
    assert load_org(tmp_path).doorbell_check is None


# ---------- integration: turns resolve org dir vs workplace ----------

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


def test_two_orgs_one_repo_do_not_collide(r4t_home, tmp_path, fake_harness):
    # The A/B case: two org dirs (same repo) run as two a8s nodes; team state is
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
    assert state.team_dir("acme") != state.team_dir("beta")
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


def test_seat_resolves_the_stamped_org_dir(
    r4t_home, tmp_path, fake_harness, monkeypatch, capsys
):
    _org_dir, workplace = _stamped_org(r4t_home, tmp_path)
    cfg = _rig_config(tmp_path, fake_harness)
    monkeypatch.chdir(workplace)
    rc = r4t_main(["seat", "--node", NODE, "--rig-config", str(cfg), "--simulate-tell"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"seat: Neil on {NODE}" in out


def test_chat_resolves_the_stamped_org_dir(
    r4t_home, tmp_path, fake_harness, monkeypatch, capsys
):
    import io

    _org_dir, workplace = _stamped_org(r4t_home, tmp_path)
    cfg = _rig_config(tmp_path, fake_harness)
    monkeypatch.chdir(workplace)
    monkeypatch.setattr(sys, "stdin", io.StringIO("/quit\n"))
    rc = r4t_main([
        "chat", "--plain", "--node", NODE, "--rig-config", str(cfg), "--simulate-tell",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"seat: Neil on {NODE}" in out


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


def test_seat_adopts_the_root_when_no_stamp_exists(
    r4t_home, tmp_path, fake_harness, monkeypatch, capsys
):
    # The live quill sequence: a team driven entirely through the seat never
    # passes cmd_dispatch, so no stamp exists and observer commands guess
    # from cwd. One seat run with --root writes the stamp; from then on the
    # workplace cwd resolves the node and the org dir.
    org_dir, workplace = _portable_org(tmp_path)
    cfg = _rig_config(tmp_path, fake_harness)
    assert state.read_root(NODE) is None

    rc = r4t_main([
        "seat", "--node", NODE, "--root", str(org_dir),
        "--rig-config", str(cfg), "--simulate-tell",
    ])
    assert rc == 0
    assert state.read_root(NODE) == org_dir

    state.team_dir("other").mkdir(parents=True)  # ambiguity is real
    monkeypatch.chdir(workplace)
    capsys.readouterr()
    rc = r4t_main(["status", "--rig-config", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"team: {NODE}" in out and "Gerry" in out


def test_seat_never_overrides_an_existing_stamp(
    r4t_home, tmp_path, fake_harness, capsys
):
    org_dir, workplace = _portable_org(tmp_path)
    state.stamp_root(NODE, org_dir)
    (workplace / "ROSTER.md").write_text(SHADOW_ROSTER, encoding="utf-8")
    cfg = _rig_config(tmp_path, fake_harness)
    rc = r4t_main([
        "seat", "--node", NODE, "--root", str(workplace),
        "--rig-config", str(cfg), "--simulate-tell",
    ])
    assert rc == 2  # shadow roster has no human — the seat refuses
    assert state.read_root(NODE) == org_dir  # and the stamp is untouched
