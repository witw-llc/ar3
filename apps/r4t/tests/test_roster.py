from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from roster import (
    RosterError,
    load_roster,
    parse_roster,
    resolve_roster_path,
)

CONTINUE_TEXT = textwrap.dedent(
    """\
    ### Ana
    - **Rig:** r
    - **Continue:** on

    ### Bob
    - **Rig:** r

    ### Cid
    - **Rig:** r
    - **Continue:** off
    """
)


def parse(text: str):
    return parse_roster(text, Path("ROSTER.md"))


TREE_TEXT = textwrap.dedent(
    """\
    ### Vic
    - **Rig:** r
    - **Leader:** yes
    - **Cell:** lead
    - **Lead:** Ned

    ### Ned
    - **Human:** yes
    - **Address:** ned

    ### Ann
    - **Rig:** r
    - **Cell:** design
    - **Lead:** Vic

    ### Bea
    - **Rig:** r
    - **Cell:** design
    - **Lead:** Ann

    ### Cal
    - **Rig:** r
    - **Cell:** build
    - **Lead:** Vic
    """
)


class TestParsing:
    def test_basic_fields(self, repo):
        roster = load_roster(repo / "ROSTER.md")
        gerry = roster.find("gerry")
        assert gerry is not None
        assert gerry.is_human is False
        assert gerry.rig == "leader"
        assert gerry.role == "Technical Producer"
        assert gerry.leader
        assert not gerry.errors
        assert "Defends the schedule" in gerry.persona

    def test_cell_captured_when_declared(self, repo):
        gerry = load_roster(repo / "ROSTER.md").find("gerry")
        assert gerry.cell == "leadership"

    def test_cell_empty_when_absent(self, repo):
        phil = load_roster(repo / "ROSTER.md").find("phil")
        assert phil.cell == ""

    def test_continue_on_is_read(self):
        assert parse(CONTINUE_TEXT).find("ana").continue_conversation is True

    def test_continue_defaults_off_when_absent(self):
        assert parse(CONTINUE_TEXT).find("bob").continue_conversation is False

    def test_continue_off_is_off(self):
        assert parse(CONTINUE_TEXT).find("cid").continue_conversation is False

    def test_continue_does_not_disturb_other_fields(self):
        ana = parse(CONTINUE_TEXT).find("ana")
        assert ana.rig == "r"
        assert not ana.errors

    def test_continue_on_has_no_flush_window(self):
        assert parse(CONTINUE_TEXT).find("ana").flush_seconds is None

    def test_continue_bare_seconds(self):
        member = parse("### A\n- **Rig:** r\n- **Continue:** 90\n").find("a")
        assert member.continue_conversation is True
        assert member.flush_seconds == 90.0

    @pytest.mark.parametrize(
        "value,seconds",
        [("30s", 30.0), ("5m", 300.0), ("4h", 14400.0), ("2d", 172800.0)],
    )
    def test_continue_duration_suffixes(self, value, seconds):
        member = parse(f"### A\n- **Rig:** r\n- **Continue:** {value}\n").find("a")
        assert member.continue_conversation is True
        assert member.flush_seconds == seconds

    def test_continue_off_has_no_flush_window(self):
        assert parse(CONTINUE_TEXT).find("cid").flush_seconds is None

    @pytest.mark.parametrize("value", ["0", "0m", "-5m"])
    def test_continue_non_positive_duration_disables_member(self, value):
        member = parse(f"### A\n- **Rig:** r\n- **Continue:** {value}\n").find("a")
        assert "Continue must be on, off, or an idle window" in member.error
        assert member.continue_conversation is False

    def test_continue_garbage_disables_member(self):
        member = parse("### A\n- **Rig:** r\n- **Continue:** soon\n").find("a")
        assert "Continue must be on, off, or an idle window" in member.error
        assert member.continue_conversation is False

    def test_fallback_defaults_on_when_absent(self):
        assert parse("### A\n- **Rig:** r\n").find("a").fallback is True

    @pytest.mark.parametrize("value", ["on", "yes"])
    def test_fallback_on(self, value):
        member = parse(f"### A\n- **Rig:** r\n- **Fallback:** {value}\n").find("a")
        assert member.fallback is True
        assert not member.errors

    @pytest.mark.parametrize("value", ["off", "no"])
    def test_fallback_off(self, value):
        member = parse(f"### A\n- **Rig:** r\n- **Fallback:** {value}\n").find("a")
        assert member.fallback is False
        assert not member.errors

    def test_fallback_garbage_disables_member(self):
        member = parse("### A\n- **Rig:** r\n- **Fallback:** maybe\n").find("a")
        assert "Fallback must be on or off" in member.error
        assert "(try: Fallback: off)" in member.error

    def test_flush_field_disables_member(self):
        member = parse(
            "### A\n- **Rig:** r\n- **Continue:** on\n- **Flush:** 4h\n"
        ).find("a")
        assert "Flush: is not a field" in member.error
        assert "(try: Continue: 15m)" in member.error

    def test_lookup_is_case_insensitive(self, repo):
        roster = load_roster(repo / "ROSTER.md")
        assert roster.find("PHIL") is not None
        assert roster.find("Phil") is roster.find("phil")

    def test_unknown_member(self, repo):
        roster = load_roster(repo / "ROSTER.md")
        assert roster.find("nobody") is None

    def test_leader(self, repo):
        roster = load_roster(repo / "ROSTER.md")
        assert roster.leader().name == "Gerry"

    def test_no_leader(self):
        roster = parse("### Solo\n- **Rig:** t\n")
        assert roster.leader() is None

    def test_human_leader_not_dispatched_as_leader(self):
        roster = parse(
            "### Boss\n- **Human:** yes\n- **Leader:** yes\n"
            "### Dev\n- **Rig:** t\n"
        )
        assert roster.leader() is None

    def test_human(self, repo):
        neil = load_roster(repo / "ROSTER.md").find("neil")
        assert neil.is_human
        assert neil.address == "neil"
        assert not neil.errors

    def test_human_needs_no_harness(self):
        roster = parse("### Human\n- **Human:** yes\n")
        assert not roster.find("human").errors

    def test_unmarked_member_is_ai(self):
        member = parse("### A\n- **Rig:** t\n").find("a")
        assert member.is_human is False
        assert not member.errors

    def test_human_no_is_ai(self):
        assert parse("### A\n- **Human:** no\n- **Rig:** t\n").find("a").is_human is False

    def test_backticked_rig(self):
        roster = parse("### A\n- **Rig:** `rig-1`\n")
        assert roster.find("a").rig == "rig-1"

    def test_rig_is_lowercased(self):
        roster = parse("### A\n- **Rig:** Leader\n")
        assert roster.find("a").rig == "leader"

    def test_workdir_captured(self):
        roster = parse(
            "### A\n- **Rig:** t\n- **Workdir:** agents/bob\n"
        )
        assert roster.find("a").workdir == "agents/bob"

    def test_workdir_empty_when_absent(self, repo):
        assert load_roster(repo / "ROSTER.md").find("phil").workdir == ""

    def test_mandate_accepted_as_role(self):
        roster = parse(
            "### A\n- **Rig:** t\n- **Mandate:** The Server\n"
        )
        assert roster.find("a").role == "The Server"

    def test_blocks_end_at_next_heading(self):
        roster = parse(
            "### A\n- **Rig:** t\npersona a\n"
            "## Section\nloose prose\n"
            "### B\n- **Rig:** t\n"
        )
        assert "loose prose" not in roster.find("a").persona
        assert roster.find("b") is not None


class TestMalformed:
    def test_bad_rig_disables_member(self, repo):
        roster = load_roster(repo / "ROSTER.md")
        broken = roster.find("broken")
        assert broken.errors
        assert "symbolic rig" in broken.error

    def test_human_with_rig_disabled(self):
        member = parse("### A\n- **Human:** yes\n- **Rig:** solo\n").find("a")
        assert member.rig is None
        assert "Human members carry no Rig" in member.error

    def test_legacy_status_line_disables_member(self):
        member = parse("### A\n- **Status:** Human\n").find("a")
        assert member.error == (
            "Status: is gone — mark the human seat with **Human:** yes; "
            "AI members carry no marker; missing Rig line"
        )

    def test_legacy_status_value_is_never_parsed(self):
        assert parse("### A\n- **Status:** Human\n").find("a").is_human is False

    def test_command_harness_disables_member(self):
        roster = parse(
            "### A\n- **Rig:** `agent -p --yolo {prompt}`\n"
        )
        member = roster.find("a")
        assert member.errors
        assert "symbolic rig" in member.error

    def test_ai_without_rig_disabled(self):
        roster = parse("### A\n")
        assert "missing Rig" in roster.find("a").error

    def test_duplicate_names_disable_both(self):
        roster = parse(
            "### A\n- **Rig:** t\n"
            "### a\n- **Rig:** t\n"
        )
        assert all("duplicate" in m.error for m in roster.members)

    def test_malformed_block_never_raises(self):
        roster = parse("### \n### A\n- **Human:**\n- garbage ** stuff\n")
        assert isinstance(roster.members, list)


class TestPathResolution:
    def test_default(self, tmp_path):
        assert resolve_roster_path(tmp_path, None) == tmp_path / "ROSTER.md"

    def test_root_relative(self, tmp_path):
        assert (
            resolve_roster_path(tmp_path, "docs/ROSTER.md")
            == tmp_path / "docs" / "ROSTER.md"
        )

    def test_absolute(self, tmp_path):
        target = tmp_path / "elsewhere.md"
        assert resolve_roster_path(tmp_path / "repo", str(target)) == target

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(RosterError):
            load_roster(tmp_path / "nope.md")


class TestTree:
    def test_lead_parsed(self):
        r = parse(TREE_TEXT)
        assert r.find("ann").lead == "Vic"
        assert r.find("vic").lead == "Ned"

    def test_lead_empty_when_absent(self):
        assert parse("### A\n- **Rig:** r\n").find("a").lead == ""

    def test_declares_tree_only_with_lead_lines(self):
        assert parse(TREE_TEXT).declares_tree
        flat = parse(
            "### A\n- **Rig:** r\n- **Leader:** yes\n- **Cell:** x\n"
            "### B\n- **Rig:** r\n- **Cell:** x\n"
        )
        assert not flat.declares_tree

    def test_reports_to(self):
        r = parse(TREE_TEXT)
        assert {m.name for m in r.reports_to(r.find("vic"))} == {"Ann", "Cal"}
        assert {m.name for m in r.reports_to(r.find("ann"))} == {"Bea"}

    def test_adjacent_is_lead_reports_cellmates_and_seat(self):
        r = parse(TREE_TEXT)
        adj = {m.name for m in r.adjacent(r.find("ann"))}
        # lead (Vic), report+cell-mate (Bea), human seat (Ned) — never Cal
        assert adj == {"Vic", "Bea", "Ned"}
        assert "Cal" not in adj

    def test_adjacent_top_lead_sees_reports_and_seat(self):
        r = parse(TREE_TEXT)
        adj = {m.name for m in r.adjacent(r.find("vic"))}
        assert {"Ann", "Cal", "Ned"} <= adj
        assert "Bea" not in adj  # Bea is two levels down, not adjacent

    def test_tree_text_is_clean(self):
        assert parse(TREE_TEXT).tree_problems() == []

    def test_flat_roster_has_no_tree_problems(self):
        # Cell lines but no Lead lines anywhere: many members, still flat.
        text = "### Top\n- **Rig:** r\n- **Leader:** yes\n"
        for i in range(12):
            text += f"### M{i}\n- **Rig:** r\n"
        assert parse(text).tree_problems() == []

    def test_unknown_lead_is_error(self):
        r = parse(
            "### Top\n- **Rig:** r\n- **Leader:** yes\n"
            "### Kid\n- **Rig:** r\n- **Lead:** Ghost\n"
        )
        assert any(s == "error" and "Ghost" in msg for s, msg in r.tree_problems())

    def test_cell_over_six_warns(self):
        text = "### Top\n- **Rig:** r\n- **Leader:** yes\n- **Cell:** hq\n"
        for i in range(7):
            text += f"### M{i}\n- **Rig:** r\n- **Cell:** c\n- **Lead:** Top\n"
        probs = parse(text).tree_problems()
        assert any(s == "warn" and "soft cap 6" in msg for s, msg in probs)
        assert not any(s == "error" for s, _ in probs)

    def test_cell_over_ten_errors(self):
        text = "### Top\n- **Rig:** r\n- **Leader:** yes\n- **Cell:** hq\n"
        for i in range(11):
            text += f"### M{i}\n- **Rig:** r\n- **Cell:** c\n- **Lead:** Top\n"
        assert any(
            s == "error" and "hard cap 10" in msg for s, msg in parse(text).tree_problems()
        )

    def test_depth_over_two_warns(self):
        r = parse(
            "### L0\n- **Rig:** r\n- **Leader:** yes\n"
            "### L1\n- **Rig:** r\n- **Lead:** L0\n"
            "### L2\n- **Rig:** r\n- **Lead:** L1\n"
            "### L3\n- **Rig:** r\n- **Lead:** L2\n"
        )
        assert any(s == "warn" and "depth" in msg for s, msg in r.tree_problems())
