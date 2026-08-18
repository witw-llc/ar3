"""The runbook — one `r4t.md` that says what the team is.

Covers the design's validation table (every loud error), the two worked
examples, the rig-shadowing ruling, `${VAR}` fail-closed, the `extends` chain,
and the read path: a node carrying a runbook dispatches from it.
"""
from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

import pytest

import runbook
from rig import A8S_PY, load_rig_config
from roster import RosterError, load_roster, resolve_roster_path
from runbook import NodeVars, RunbookError, load_runbook

TRIFORCE = runbook.BUILTIN_DIR / "triforce.md"
ARK_SUITE = runbook.BUILTIN_DIR / "ark-suite.md"


def write(root: Path, text: str, name: str = runbook.RUNBOOK_NAME) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


MINIMAL = """
    ---
    name: "acme"
    ---

    ## Roster

    ### Lead
    - **Engine:** claude --model opus
    - **Leader:** yes
    - **Role:** Talks to the owner

    You are the lead.
"""


@pytest.fixture
def node(tmp_path):
    root = tmp_path / "node"
    root.mkdir()
    return root


@pytest.fixture
def trusted(r4t_home):
    """A node name whose machine trust ceiling is raised, the way
    `r4t add --trust` raises it. A runbook asking for `bypass` needs one."""
    from rig import raise_machine_ceiling

    raise_machine_ceiling("trusted")
    return "trusted"


# --- the built-ins ----------------------------------------------------------


class TestBuiltins:
    def test_both_ship(self):
        assert set(runbook.builtin_names()) >= {"triforce", "ark-suite"}

    def test_triforce_is_the_worked_example(self):
        """§3's expectation, decision for decision: three engine lines, one
        leader, two leads, two rituals — and zero abstractions, which is the
        starter arguing for itself."""
        book = load_runbook(TRIFORCE)
        assert book.name == "triforce"
        assert [m.name for m in book.roster.members] == ["Lead", "Dev", "Critic"]
        assert [m.name for m in book.roster.members if m.leader] == ["Lead"]
        assert [m.lead for m in book.roster.members] == ["", "Lead", "Lead"]
        assert all(m.rig_override is not None for m in book.roster.members)
        assert all(not m.errors for m in book.roster.members)
        assert book.rigs == {}
        assert book.cells == {}
        assert sorted(book.rituals) == ["mission-review", "standup"]
        assert book.rituals["standup"].when == "weekdays 09:00"
        assert book.rituals["mission-review"].when == "on idle"
        assert not any(r.errors for r in book.rituals.values())
        assert any("does not run them" in w for w in book.warnings)

    def test_triforce_engine_lines_resolve_to_runnable_rigs(self):
        book = load_runbook(TRIFORCE)
        lead = book.roster.find("Lead")
        assert lead.rig_override.preset == "claude"
        assert lead.rig_override.error is None
        assert "opus" in " ".join(lead.rig_override.pool()[0])

    def test_the_shipped_mission_is_the_default(self):
        book = load_runbook(TRIFORCE)
        assert "Done beats perfect" in book.mission
        assert "ASD-STE100" in book.mission
        assert book.charter.startswith("How this team works")

    def test_ark_suite_extends_triforce_and_replaces_the_charter(self):
        book = load_runbook(ARK_SUITE)
        assert book.chain == ["triforce", "ark-suite"]
        assert book.source_of("Charter") == "ark-suite"
        assert book.source_of("Mission") == "triforce"
        assert book.source_of("Roster") == "triforce"
        assert "The merge ladder has four rungs" in book.charter
        assert [m.name for m in book.roster.members] == ["Lead", "Dev", "Critic"]

    def test_every_builtin_is_a_valid_runbook(self):
        for name in runbook.builtin_names():
            book = load_runbook(runbook.BUILTIN_DIR / f"{name}.md")
            assert book.roster.leader_problem() is None, name
            assert not [m.name for m in book.roster.members if m.errors], name


# --- ar3's own roster -------------------------------------------------------

AR3_ROOT = Path(__file__).resolve().parents[3] / "org" / "AR3"


@pytest.mark.skipif(
    not (AR3_ROOT / runbook.RUNBOOK_NAME).is_file(),
    reason="org/ is the suite's own operations and never leaves the private repo",
)
class TestArksOwnRoster:
    """The acceptance test the format was designed against: six files in
    three formats became one `r4t.md`, and the roster that develops ar3 has
    to load with nothing wrong and nothing worth warning about."""

    @pytest.fixture
    def book(self):
        return load_runbook(AR3_ROOT / runbook.RUNBOOK_NAME)

    def test_it_loads_with_no_error_and_only_the_ritual_notice(self, book):
        assert book.roster.leader_problem() is None
        assert [m.name for m in book.roster.members if m.errors] == []
        assert [name for name, rig in book.rigs.items() if rig.error] == []
        assert [c.name for c in book.cells.values() if c.errors] == []
        assert [r.name for r in book.rituals.values() if r.errors] == []
        assert book.warnings == [
            "rituals (mission-review, weekly-review) are declared and "
            "validated; this release does not run them — the idle mission "
            "review is built-in behavior, not a ritual block"
        ]

    def test_it_stands_alone(self, book):
        """It would override every section it could inherit, so `extends:`
        would resolve to zero lines and cost a reader two more files to
        prove it."""
        assert book.chain == ["r4t.md"]
        assert {name: s.source for name, s in book.sections.items()} == {
            name: "r4t.md" for name in runbook.SECTIONS
        }

    def test_the_whole_roster_survived_the_collapse(self, book):
        assert [m.name for m in book.roster.members] == [
            "Mira", "Nora", "Tess", "Silas", "Juno",
        ]
        assert book.roster.leader().name == "Mira"
        assert [m.lead for m in book.roster.members] == [
            "", "Mira", "Nora", "Mira", "Silas",
        ]
        assert all(m.knowledge_on for m in book.roster.members)
        assert sorted(book.cells) == ["build", "leadership", "product"]
        assert sorted(book.rigs) == [
            "ark-eng-claude", "ark-eng-cursor", "ark-generalist", "ark-lead",
        ]
        assert sorted(book.rituals) == ["mission-review", "weekly-review"]
        assert "The throttle on all AI work is the human brain" in book.mission
        assert "A finding is not a fact until something tried to kill it" in book.charter

    def test_the_frontmatter_carries_what_the_org_config_did(self):
        from org import load_org

        org = load_org(AR3_ROOT)
        assert org.workplace == AR3_ROOT.parents[1], "workdir: ../.. is the repo"
        assert org.is_portable
        assert org.comms == "open"
        assert org.egress is True

    def test_a_rig_block_states_the_argv_the_members_run(self, book):
        """The `Allowed tools:` line is the reason these rigs exist: bare
        `Bash`, because the members develop ar3 and need git and python."""
        lead = book.rigs["ark-lead"]
        assert lead.argv("GO") == [
            "claude", "--model", "opus",
            "--permission-mode", "dontAsk",
            "--allowedTools",
            "Bash Read Edit Write Glob Grep WebFetch WebSearch TodoWrite",
            "--exclude-dynamic-system-prompt-sections", "-p", "GO",
        ]
        assert (lead.rig_budget_earn_per_hour, lead.rig_budget_max) == (12.0, 12.0)
        assert book.rigs["ark-generalist"].model_resolver == "agy-live"

    def test_the_files_it_replaced_are_gone(self):
        assert runbook.legacy_conflict(AR3_ROOT) is None
        for stale in ("rigs.json", "definition.json"):
            assert not (AR3_ROOT / stale).exists(), stale


# --- the block grammar ------------------------------------------------------


class TestGrammar:
    def test_a_bare_engine_line_parses_as_dictated(self, node):
        """The owner wrote `engine: claude --model opus` unbolded. Rejecting
        that spelling is exactly the friction the format exists to remove."""
        write(node, """
            ## Roster

            ### Lead
            - engine: claude --model opus
            - leader: yes
        """)
        book = runbook.load_for_root(node)
        lead = book.roster.find("Lead")
        assert lead.errors == []
        assert lead.rig_override.preset == "claude"

    @pytest.mark.parametrize(
        "spelling",
        ["- **Allowed tools:** Read", "- **allowed_tools:** Read", "- allowedtools: Read"],
    )
    def test_keys_ignore_case_space_and_underscore(self, node, spelling):
        write(node, f"""
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes

            ## Rigs

            ### spare
            - **Engine:** claude
            {spelling}
        """)
        book = runbook.load_for_root(node)
        assert book.rigs["spare"].allowed_tools == "Read"

    def test_prose_after_the_fields_is_the_payload(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes

            You are the lead. Route every question.
        """)
        book = runbook.load_for_root(node)
        lead = book.roster.find("Lead")
        assert lead.persona.splitlines()[0] == "### Lead"
        assert "Route every question." in lead.persona

    def test_prose_under_a_collection_section_is_ignored(self, node):
        write(node, """
            ## Roster

            Members are `###` blocks. Names are addresses.

            - **Do not A/B member tiers here.** This roster is an instrument.

            ### Lead
            - **Engine:** claude
            - **Leader:** yes
        """)
        book = runbook.load_for_root(node)
        assert [m.name for m in book.roster.members] == ["Lead"]

    def test_a_repeated_field_that_is_not_env_is_an_error(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Engine:** codex
            - **Leader:** yes
        """)
        book = runbook.load_for_root(node, validate=False)
        assert any("set 2 times" in e for e in book.roster.find("Lead").errors)

    def test_env_repeats(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Rig:** big
            - **Leader:** yes

            ## Rigs

            ### big
            - **Engine:** claude
            - **Env:** ENABLE_PROMPT_CACHING_1H=1
            - **Env:** OTHER=2
        """)
        book = runbook.load_for_root(node)
        assert book.rigs["big"].env == {"ENABLE_PROMPT_CACHING_1H": "1", "OTHER": "2"}


# --- the engine line --------------------------------------------------------


class TestEngineLine:
    def test_the_closed_flag_set(self, node, trusted):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude --model opus --permissions bypass --timeout 60
            - **Leader:** yes
        """)
        rig = runbook.load_for_root(node, node=trusted).roster.find("Lead").rig_override
        assert rig.permissions == "bypass"
        assert rig.timeout_seconds == 60

    def test_an_unknown_engine_names_the_known_ones(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** gpt9000
            - **Leader:** yes
        """)
        err = "; ".join(runbook.load_for_root(node, validate=False).roster.find("Lead").errors)
        assert "is not an engine" in err
        assert "claude" in err

    def test_an_unknown_flag_names_the_known_ones(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude --temperature 0.2
            - **Leader:** yes
        """)
        err = "; ".join(runbook.load_for_root(node, validate=False).roster.find("Lead").errors)
        assert "--temperature" in err and "--allowed-tools" in err

    def test_continue_is_a_member_field_not_an_engine_flag(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude --continue
            - **Leader:** yes
        """)
        err = "; ".join(runbook.load_for_root(node, validate=False).roster.find("Lead").errors)
        assert "Continue:" in err

    def test_a_stance_the_preset_cannot_reach_fails_the_rig_closed(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** ollama --model qwen3:0.6b --allowed-tools Read
            - **Leader:** yes
        """)
        assert runbook.load_for_root(node, validate=False).roster.find("Lead").errors


# --- rigs, and the shadowing ruling -----------------------------------------


class TestRigs:
    def test_a_runbook_rig_shadows_a_machine_rig_of_the_same_name(
        self, node, r4t_home, rig_config
    ):
        """The determinism ruling on #160. Whole-block, never field-merged: a
        runbook rig that silently inherited a stance from the machine's rig of
        the same name is the outcome the format exists to prevent."""
        write(node, """
            ## Roster

            ### Lead
            - **Rig:** leader
            - **Leader:** yes

            ## Rigs

            ### leader
            - **Engine:** codex --permissions auto
        """)
        machine = load_rig_config(rig_config)
        assert machine.rigs["leader"].preset is None

        member = runbook.load_for_root(node).roster.find("Lead")
        rig, err, pinned = machine.rig_for(member)
        assert err is None and not pinned
        assert rig.name == "leader"
        assert rig.preset == "codex"
        assert rig.permissions == "auto"

    def test_a_rig_the_runbook_does_not_declare_falls_through_to_the_machine(
        self, node, r4t_home, rig_config
    ):
        write(node, """
            ## Roster

            ### Lead
            - **Rig:** junior-dev
            - **Leader:** yes
        """)
        member = runbook.load_for_root(node).roster.find("Lead")
        assert member.rig_override is None
        rig, err, _pinned = load_rig_config(rig_config).rig_for(member)
        assert err is None and rig.name == "junior-dev"

    def test_an_inline_engine_needs_no_machine_config_at_all(self, node, tmp_path):
        write(node, MINIMAL)
        member = runbook.load_for_root(node).roster.find("Lead")
        rig, err, _pinned = load_rig_config(tmp_path / "absent.json").rig_for(member)
        assert err is None and rig.preset == "claude"

    def test_a_pin_still_wins(self, node, r4t_home, rig_config):
        write(node, """
            ## Roster

            ### Gerry
            - **Engine:** codex
            - **Leader:** yes
        """)
        member = runbook.load_for_root(node).roster.find("Gerry")
        rig, err, pinned = load_rig_config(rig_config).rig_for(member)
        assert pinned and err is None and rig.name == "leader"

    def test_the_rig_body_is_the_member_body(self, node, trusted):
        write(node, """
            ## Roster

            ### Lead
            - **Rig:** ark-lead
            - **Leader:** yes

            ## Rigs

            ### ark-lead
            - **Engine:** claude --model opus --permissions bypass
            - **Allowed tools:** Bash Read Edit Write
            - **Rig budget:** 12 per hour, max 12
            - **Member budget:** 8 per hour, max 8
            - **Max sends:** 4
            - **MCP:** on

            The lead's rig.
        """)
        rig = runbook.load_for_root(node, node=trusted).rigs["ark-lead"]
        assert rig.error is None
        assert rig.permissions == "bypass"
        assert rig.allowed_tools == "Bash Read Edit Write"
        assert (rig.rig_budget_earn_per_hour, rig.rig_budget_max) == (12, 12)
        assert (rig.budget_earn_per_hour, rig.budget_max) == (8, 8)
        assert rig.max_sends_per_turn == 4
        assert rig.mcp is True

    def test_a_malformed_budget_says_the_shape(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Rig:** spare
            - **Leader:** yes

            ## Rigs

            ### spare
            - **Engine:** claude
            - **Rig budget:** lots
        """)
        assert "8 per hour, max 16" in runbook.load_for_root(node).rigs["spare"].error

    def test_both_allowlist_spellings_at_once_is_refused(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Rig:** spare
            - **Leader:** yes

            ## Rigs

            ### spare
            - **Engine:** claude --allowed-tools Read
            - **Allowed tools:** Bash
        """)
        assert "delete one" in runbook.load_for_root(node).rigs["spare"].error

    @pytest.mark.parametrize("value,expected", [("y", True), ("0", False), ("N", False)])
    def test_mcp_and_echo_accept_the_full_loose_vocabulary(self, node, value, expected):
        # MCP:/Echo: used to accept only on/yes/true and off/no/false — a
        # narrower set than every other runbook boolean. One vocabulary now.
        write(node, f"""
            ## Roster

            ### Lead
            - **Rig:** spare
            - **Leader:** yes

            ## Rigs

            ### spare
            - **Engine:** claude
            - **MCP:** {value}
            - **Echo:** {value}
        """)
        rig = runbook.load_for_root(node).rigs["spare"]
        assert rig.error is None
        assert rig.mcp is expected
        assert rig.echo is expected

    def test_mcp_garbage_is_a_field_error(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Rig:** spare
            - **Leader:** yes

            ## Rigs

            ### spare
            - **Engine:** claude
            - **MCP:** maybe
        """)
        assert (
            "MCP must be yes/no/true/false/y/n/1/0/on/off"
            in runbook.load_for_root(node).rigs["spare"].error
        )


# --- validation -------------------------------------------------------------


class TestValidation:
    def test_no_leader_refuses_the_load(self, node):
        write(node, """
            ## Roster

            ### Dev
            - **Engine:** claude
        """)
        with pytest.raises(RunbookError, match="marks no leader"):
            runbook.load_for_root(node)

    def test_two_leaders_refuse_the_load(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes

            ### Other
            - **Engine:** claude
            - **Leader:** yes
        """)
        with pytest.raises(RunbookError, match="marks 2 leaders"):
            runbook.load_for_root(node)

    def test_an_unknown_section_names_the_six(self, node):
        write(node, """
            ## Mission
            x

            ## Squad
            y
        """)
        with pytest.raises(RunbookError, match=r"unknown section `## Squad`.*Rituals"):
            runbook.load_for_root(node)

    def test_a_repeated_section_is_refused(self, node):
        write(node, """
            ## Mission
            x

            ## Mission
            y
        """)
        with pytest.raises(RunbookError, match="appears twice"):
            runbook.load_for_root(node)

    def test_an_unknown_field_names_the_set(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes
            - **Rigs:** typo
        """)
        err = "; ".join(runbook.load_for_root(node).roster.find("Lead").errors)
        assert "unknown member field 'rigs'" in err and "rig" in err

    def test_cell_ingress_garbage_is_a_field_error(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes

            ## Cells

            ### product
            - **Lead:** Lead
            - **Ingress:** maybe
        """)
        err = "; ".join(runbook.load_for_root(node).cells["product"].errors)
        assert "Ingress must be yes/no/true/false/y/n/1/0/on/off" in err

    def test_a_member_with_both_engine_and_rig_is_refused(self, node):
        write(node, """
            ## Roster

            ### Mira
            - **Engine:** claude --model opus
            - **Rig:** ark-lead
            - **Leader:** yes
        """)
        err = "; ".join(runbook.load_for_root(node).roster.find("Mira").errors)
        assert "carries both Engine: and Rig:" in err
        assert "'ark-lead'" in err and "claude --model opus" in err

    def test_a_member_with_neither_has_nothing_to_run(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Leader:** yes
            - **Role:** talks
        """)
        err = "; ".join(runbook.load_for_root(node).roster.find("Lead").errors)
        assert "names neither Engine: nor Rig:" in err

    def test_lead_must_name_a_member(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes

            ### Dev
            - **Engine:** claude
            - **Lead:** Nobody
        """)
        err = "; ".join(runbook.load_for_root(node).roster.find("Dev").errors)
        assert "Lead 'Nobody' is not a member" in err

    def test_a_cell_section_makes_cell_names_authoritative(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes
            - **Cell:** typo

            ## Cells

            ### leadership
            - **Lead:** Lead
        """)
        err = "; ".join(runbook.load_for_root(node).roster.find("Lead").errors)
        assert "is not declared in `## Cells`" in err

    def test_no_cell_section_means_free_form_labels(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes
            - **Cell:** whatever
        """)
        assert runbook.load_for_root(node).roster.find("Lead").errors == []

    def test_a_ritual_target_must_resolve(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes

            ## Rituals

            ### standup
            - **When:** weekdays 09:00
            - **To:** Ghost

            Say something.
        """)
        err = "; ".join(runbook.load_for_root(node).rituals["standup"].errors)
        assert "names neither a member nor a cell" in err

    def test_the_schedule_vocabulary_is_closed(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes

            ## Rituals

            ### standup
            - **When:** 0 9 * * 1-5
            - **To:** Lead
        """)
        err = "; ".join(runbook.load_for_root(node).rituals["standup"].errors)
        assert "there is no cron form" in err

    def test_a_duplicate_member_is_named(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes

            ### Lead
            - **Engine:** codex
        """)
        book = runbook.load_for_root(node, validate=False)
        assert all("duplicate roster entry" in m.errors for m in book.roster.members)

    def test_a_member_named_like_a_cell_is_refused(self, node):
        write(node, """
            ## Roster

            ### build
            - **Engine:** claude
            - **Leader:** yes
            - **Cell:** build

            ## Cells

            ### build
            - **Lead:** build
        """)
        err = "; ".join(runbook.load_for_root(node).roster.find("build").errors)
        assert "names both a member and a cell" in err

    @pytest.mark.parametrize("name", [":clancy", "cla:ncy", "clancy:"])
    def test_a_colon_in_a_member_name_is_refused(self, node, name):
        write(node, f"""
            ## Roster

            ### {name}
            - **Engine:** claude
            - **Leader:** yes
        """)
        err = "; ".join(runbook.load_for_root(node).roster.members[0].errors)
        assert "contains a colon" in err

    def test_a_colon_in_a_cell_name_is_refused(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes

            ## Cells

            ### lead:ership
            - **Lead:** Lead
        """)
        cell = runbook.load_for_root(node).cells["lead:ership"]
        assert any("contains a colon" in e for e in cell.errors)

    def test_the_name_charset_is_the_a8s_one(self):
        source = (A8S_PY.parent / "core.py").read_text(encoding="utf-8")
        m = re.search(r"^NAME_RE = re\.compile\((.+)\)$", source, re.M)
        assert m, "a8s no longer defines NAME_RE where the twin expects it"
        assert ast.literal_eval(m.group(1)) == runbook.NAME_RE.pattern

    @pytest.mark.parametrize(
        "line,needle",
        [
            ("- **Human:** yes", "the node is the apex"),
            ("- **Address:** neil", "a8s's job"),
            ("- **Status:** active", "members carry no marker"),
            ("- **Flush:** 15m", "rides Continue:"),
            ("- **Fallback:** off", "now ProseReply:"),
        ],
    )
    def test_a_removed_field_says_what_replaced_it(self, node, line, needle):
        write(node, f"""
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes
            {line}
        """)
        err = "; ".join(runbook.load_for_root(node).roster.find("Lead").errors)
        assert needle in err

    @pytest.mark.parametrize(
        "line,needle",
        [
            ("- **Remove:** yes", "no tombstones and no H3-level merge"),
            ("- **Concurrency:** 2", "one live turn per node"),
        ],
    )
    def test_a_deferred_field_says_deferred_not_unknown(self, node, line, needle):
        write(node, f"""
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes

            ## Rigs

            ### spare
            - **Engine:** claude
            {line}
        """)
        assert needle in runbook.load_for_root(node).rigs["spare"].error

    def test_a_cell_budget_is_deferred_by_name(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes
            - **Cell:** leadership

            ## Cells

            ### leadership
            - **Lead:** Lead
            - **Budget:** 8 per hour, max 16
        """)
        err = "; ".join(runbook.load_for_root(node).cells["leadership"].errors)
        assert "deferred" in err and "cell_budget_max" in err


class TestWarnings:
    def test_a_rig_nobody_names_is_dead_weight(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes

            ## Rigs

            ### orphan
            - **Engine:** claude
        """)
        assert any("no member names it" in w for w in runbook.load_for_root(node).warnings)

    def test_a_cell_nobody_joins_is_named(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes

            ## Cells

            ### product
            - **Lead:** Lead
        """)
        assert any("no member joins it" in w for w in runbook.load_for_root(node).warnings)

    def test_a_second_ingress_door_is_named_once(self, node):
        write(node, """
            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes

            ### Dev
            - **Engine:** claude
            - **Ingress:** on
        """)
        book = runbook.load_for_root(node)
        assert book.roster.find("Lead").ingress is True
        assert book.roster.find("Dev").ingress is True
        assert [w for w in book.warnings if "second door" in w] == [
            "Dev has Ingress: on and is not the leader — that is a second door "
            "into the roster"
        ]

    def test_an_unknown_frontmatter_key_warns_by_name_not_silence(self, node):
        # Unlike an unknown block key (a hard error), frontmatter is the org
        # seam and org.py reads it ad hoc — a misspelled key degrades to the
        # default rather than failing the load, but it must not vanish.
        write(node, """
            ---
            name: "acme"
            eggress: false
            ---

            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes
        """)
        book = runbook.load_for_root(node)
        assert book.roster.leader().name == "Lead"  # loads fine
        warnings = [w for w in book.warnings if "eggress" in w]
        assert len(warnings) == 1
        assert "frontmatter key 'eggress' is not recognized" in warnings[0]
        assert "egress" in warnings[0]  # the accepted set is named too


# --- variables --------------------------------------------------------------


class TestVars:
    def test_an_unset_var_with_no_default_is_a_hard_error(self, node):
        path = write(node, """
            ## Roster

            ### Lead
            - **Engine:** ${ENGINE}
            - **Leader:** yes
        """)
        with pytest.raises(RunbookError, match=r"\$\{ENGINE\} is not set"):
            load_runbook(path, vars=NodeVars(values={}))

    def test_a_default_fills_in(self, node):
        path = write(node, """
            ## Roster

            ### Lead
            - **Engine:** ${ENGINE:-claude --model opus}
            - **Leader:** yes
        """)
        book = load_runbook(path, vars=NodeVars(values={}))
        assert book.roster.find("Lead").rig_override.preset == "claude"

    def test_a_var_wins_over_its_default(self, node):
        path = write(node, """
            ## Roster

            ### Lead
            - **Engine:** ${ENGINE:-claude}
            - **Leader:** yes
        """)
        book = load_runbook(path, vars=NodeVars(values={"ENGINE": "codex"}))
        assert book.roster.find("Lead").rig_override.preset == "codex"

    def test_the_message_form_says_what_the_author_wrote(self, node):
        path = write(node, """
            ## Roster

            ### Lead
            - **Engine:** ${ENGINE:?pick an engine with a8s vars}
            - **Leader:** yes
        """)
        with pytest.raises(RunbookError, match="pick an engine with a8s vars"):
            load_runbook(path, vars=NodeVars(values={}))

    def test_the_environment_beats_the_registry(self, node, monkeypatch):
        monkeypatch.setenv("A8S_VAR_ENGINE", "codex")
        path = write(node, """
            ## Roster

            ### Lead
            - **Engine:** ${ENGINE}
            - **Leader:** yes
        """)
        book = load_runbook(path, vars=NodeVars(values={"ENGINE": "claude"}))
        assert book.roster.find("Lead").rig_override.preset == "codex"

    def test_a_heading_is_never_interpolated(self, node):
        path = write(node, """
            ## Roster

            ### ${WHO}
            - **Engine:** claude
            - **Leader:** yes
        """)
        book = load_runbook(path, vars=NodeVars(values={"WHO": "Lead"}))
        assert book.roster.members[0].name == "${WHO}"

    def test_frontmatter_is_never_interpolated(self, node):
        path = write(node, """
            ---
            name: "${NAME}"
            ---

            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes
        """)
        book = load_runbook(path, vars=NodeVars(values={"NAME": "acme"}))
        assert book.name == "${NAME}"

    def test_a_var_reaches_prose(self, node):
        path = write(node, """
            ## Charter

            Ship ${TARGET}.

            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes
        """)
        book = load_runbook(path, vars=NodeVars(values={"TARGET": "0.4"}))
        assert book.charter == "Ship 0.4."

    def test_the_mission_var_is_a_synthesized_section_at_the_top(self, node):
        path = write(node, """
            ## Mission

            The file's own mission.

            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes
        """)
        book = load_runbook(path, vars=NodeVars(values={"MISSION": "Ship 0.4."}))
        assert book.mission == "Ship 0.4."
        assert book.source_of("Mission") == "node var MISSION"

    def test_the_registry_is_read_once_per_node(self, node, monkeypatch):
        called: list[str] = []
        monkeypatch.setattr(
            runbook, "_read_a8s_vars", lambda n: called.append(n) or {}
        )
        write(node, MINIMAL)
        for _ in range(3):
            runbook.load_for_root(node, node="acme")
        assert called == ["acme"]

    def test_registry_output_parses(self, node, monkeypatch):
        class Res:
            returncode = 0
            stdout = "MISSION   Ship 0.4.\nSEAT      codex\n"

        monkeypatch.setattr(runbook.subprocess, "run", lambda *a, **k: Res())
        write(node, MINIMAL)
        book = runbook.load_for_root(node, node="acme")
        assert book.mission == "Ship 0.4."


# --- inheritance ------------------------------------------------------------


class TestExtends:
    def test_a_section_replaces_whole(self, node):
        write(node, """
            ---
            extends: "triforce"
            ---

            ## Roster

            ### Solo
            - **Engine:** codex
            - **Leader:** yes
        """)
        book = runbook.load_for_root(node)
        assert [m.name for m in book.roster.members] == ["Solo"]
        assert book.source_of("Roster") == "r4t.md"
        assert book.source_of("Charter") == "triforce"

    def test_frontmatter_merges_per_key(self, node):
        write(node, """
            ---
            name: "mine"
            extends: "triforce"
            egress: false
            ---

            ## Roster

            ### Solo
            - **Engine:** codex
            - **Leader:** yes
        """)
        book = runbook.load_for_root(node)
        assert book.frontmatter["name"] == "mine"
        assert book.frontmatter["egress"] is False
        assert book.frontmatter["comms"] == "open"  # survived from triforce

    def test_a_chain_of_three(self, node):
        write(node, """
            ---
            extends: "ark-suite"
            ---

            ## Roster

            ### Solo
            - **Engine:** codex
            - **Leader:** yes
        """)
        book = runbook.load_for_root(node)
        assert book.chain == ["triforce", "ark-suite", "r4t.md"]
        assert book.source_of("Mission") == "triforce"
        assert book.source_of("Charter") == "ark-suite"
        assert book.source_of("Roster") == "r4t.md"

    def test_a_relative_path_is_the_file_split(self, node):
        write(node, """
            ## Cells

            ### leadership
            - **Lead:** Solo
        """, name="parts/cells.md")
        write(node, """
            ---
            extends: "./parts/cells.md"
            ---

            ## Roster

            ### Solo
            - **Engine:** codex
            - **Leader:** yes
            - **Cell:** leadership
        """)
        book = runbook.load_for_root(node)
        assert sorted(book.cells) == ["leadership"]
        assert book.roster.find("Solo").errors == []

    def test_an_unresolvable_base_names_the_builtins(self, node):
        write(node, """
            ---
            extends: "nope"
            ---
        """)
        with pytest.raises(RunbookError, match="names no built-in runbook.*triforce"):
            runbook.load_for_root(node)

    def test_a_cycle_names_the_loop(self, node):
        write(node, """
            ---
            extends: "./b.md"
            ---
        """)
        write(node, """
            ---
            extends: "./r4t.md"
            ---
        """, name="b.md")
        with pytest.raises(RunbookError, match="forms a cycle"):
            runbook.load_for_root(node)

    def test_the_depth_cap(self, node):
        for i in range(runbook.MAX_EXTENDS_DEPTH + 2):
            write(node, f"""
                ---
                extends: "./link{i + 1}.md"
                ---
            """, name=f"link{i}.md")
        write(node, """
            ---
            extends: "./link0.md"
            ---
        """)
        with pytest.raises(RunbookError, match="deeper than 5"):
            runbook.load_for_root(node)


# --- show -------------------------------------------------------------------


class TestShow:
    def test_resolved_carries_no_substitution_left(self, node):
        path = write(node, """
            ---
            name: "acme"
            ---

            ## Charter

            Ship ${TARGET}.

            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes
        """)
        text = runbook.render(load_runbook(path, vars=NodeVars(values={"TARGET": "0.4"})))
        assert "${" not in text
        assert "Ship 0.4." in text

    def test_sources_annotates_every_section(self, node):
        write(node, """
            ---
            extends: "triforce"
            ---

            ## Roster

            ### Solo
            - **Engine:** codex
            - **Leader:** yes
        """)
        text = runbook.render(runbook.load_for_root(node), sources=True)
        heads = [line for line in text.splitlines() if line.startswith("## ")]
        assert heads == [
            "## Mission                                    [triforce]",
            "## Charter                                    [triforce]",
            "## Roster                                     [r4t.md]",
            "## Rituals                                    [triforce]",
        ]

    def test_sections_render_in_the_canonical_order(self, node):
        write(node, """
            ## Rituals

            ### standup
            - **When:** on idle
            - **To:** Lead

            Say something.

            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes

            ## Mission

            Ship it.
        """)
        text = runbook.render(runbook.load_for_root(node))
        heads = [line for line in text.splitlines() if line.startswith("## ")]
        assert heads == ["## Mission", "## Roster", "## Rituals"]


# --- the read path ----------------------------------------------------------


class TestReadPath:
    def test_the_runbook_is_the_roster(self, node):
        write(node, MINIMAL)
        assert resolve_roster_path(node, None).name == "r4t.md"
        roster = load_roster(resolve_roster_path(node, None))
        assert [m.name for m in roster.members] == ["Lead"]

    def test_the_runbook_wins_over_a_legacy_roster(self, node):
        write(node, MINIMAL)
        (node / "ROSTER.md").write_text(
            "### Gerry\n- **Rig:** leader\n- **Leader:** yes\n", encoding="utf-8"
        )
        assert [m.name for m in load_roster(resolve_roster_path(node, None)).members] == [
            "Lead"
        ]
        warning = runbook.legacy_conflict(node)
        assert "r4t.md" in warning and "ROSTER.md" in warning

    def test_no_runbook_keeps_the_legacy_path(self, repo):
        assert resolve_roster_path(repo, None).name == "ROSTER.md"
        assert [m.name for m in load_roster(resolve_roster_path(repo, None))
                .members] == ["Gerry", "Phil", "Broken"]

    def test_an_explicit_roster_flag_still_overrides(self, node):
        write(node, MINIMAL)
        (node / "OTHER.md").write_text(
            "### Gerry\n- **Rig:** leader\n- **Leader:** yes\n", encoding="utf-8"
        )
        assert resolve_roster_path(node, "OTHER.md").name == "OTHER.md"

    def test_a_broken_runbook_surfaces_as_a_roster_error(self, node):
        write(node, """
            ## Roster

            ### Dev
            - **Engine:** claude
        """)
        with pytest.raises(RosterError, match="marks no leader"):
            load_roster(resolve_roster_path(node, None))

    def test_frontmatter_replaces_the_org_config(self, node):
        from org import load_org

        (node.parent / "work").mkdir()
        write(node, """
            ---
            name: "acme"
            workdir: "../work"
            comms: "closed"
            egress: false
            leader_sees_lateral: true
            priority_senders: ["boss*"]
            ---

            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes
        """)
        org = load_org(node)
        assert org.workplace == (node.parent / "work").resolve()
        assert org.comms == "closed"
        assert org.egress is False
        assert org.leader_sees_lateral is True
        assert org.priority_senders == ["boss*"]
        assert org.is_portable

    def test_workdir_dot_means_the_node_dir(self, node):
        from org import load_org

        write(node, MINIMAL)
        org = load_org(node)
        assert org.workplace == node
        assert not org.is_portable

    def test_a_member_workdir_resolves_against_the_node_dir(self, node):
        from dispatch import DispatchContext, resolve_workdir
        from org import load_org

        (node.parent / "work").mkdir()
        write(node, """
            ---
            workdir: "../work"
            ---

            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes
            - **Workdir:** agents/lead
        """)
        org = load_org(node)
        ctx = DispatchContext(
            root=node,
            node="acme",
            roster_path=runbook.runbook_path(node),
            config_path=node / "rigs.json",
            tell_fn=lambda *a: None,
            workplace=org.workplace,
        )
        member = runbook.load_for_root(node).roster.find("Lead")
        assert resolve_workdir(ctx, member) == node / "agents" / "lead"

    def test_the_mission_and_charter_reach_the_prompt(self, node, r4t_home, tmp_path):
        from dispatch import DispatchContext, prompt_sections

        write(node, """
            ## Mission

            Ship the 0.4 release.

            ## Charter

            One branch per batch.

            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes

            You are the lead.
        """)
        ctx = DispatchContext(
            root=node,
            node="acme",
            roster_path=runbook.runbook_path(node),
            config_path=tmp_path / "absent.json",
            tell_fn=lambda *a: None,
            workplace=node,
        )
        book = runbook.load_for_root(node)
        member = book.roster.find("Lead")
        rig, err, _pinned = load_rig_config(ctx.config_path).rig_for(member)
        assert err is None
        sections = dict(prompt_sections(ctx, book.roster, member, [], rig))
        assert "Ship the 0.4 release." in "\n".join(sections["mission"])
        assert "One branch per batch." in "\n".join(sections["charter"])

    def test_the_charter_reaches_every_member_not_just_the_lead(self, node, r4t_home, tmp_path):
        from dispatch import DispatchContext, prompt_sections

        write(node, """
            ## Mission

            Ship the 0.4 release.

            ## Charter

            One branch per batch.

            ## Roster

            ### Lead
            - **Engine:** claude
            - **Leader:** yes

            ### Dev
            - **Engine:** claude
            - **Lead:** Lead
        """)
        ctx = DispatchContext(
            root=node,
            node="acme",
            roster_path=runbook.runbook_path(node),
            config_path=tmp_path / "absent.json",
            tell_fn=lambda *a: None,
            workplace=node,
        )
        book = runbook.load_for_root(node)
        dev = book.roster.find("Dev")
        rig, _err, _pinned = load_rig_config(ctx.config_path).rig_for(dev)
        sections = dict(prompt_sections(ctx, book.roster, dev, [], rig))
        assert "mission" not in sections
        assert "One branch per batch." in "\n".join(sections["charter"])


# --- the acceptance example -------------------------------------------------


AR3 = """
    ---
    name: "AR3"
    extends: "ark-suite"
    workdir: "../.."
    comms: "open"
    egress: true
    ---

    # AR3 — the Ark's own roster

    The suite building the suite.

    ## Mission

    Build the suite that gives one person a roster.

    ## Charter

    The chain: Owner -> Mira (the lead) -> cell leads -> members.

    ## Cells

    ### leadership
    - **Lead:** Mira
    - **Ingress:** on

    ### product
    - **Lead:** Nora

    Owns what the work is for and how it is described.

    ### build
    - **Lead:** Silas

    Owns the code, the tests, and the release machinery.

    ## Rigs

    ### ark-lead
    - **Engine:** claude --model opus --permissions bypass
    - **Allowed tools:** Bash Read Edit Write Glob Grep WebFetch WebSearch TodoWrite
    - **Rig budget:** 12 per hour, max 12

    Widened from the preset's `Bash(tell:*)` to bare `Bash`.

    ### ark-eng-claude
    - **Engine:** claude --model sonnet --permissions bypass
    - **Rig budget:** 20 per hour, max 20

    ### ark-eng-cursor
    - **Engine:** cursor --model composer-2.5 --permissions bypass
    - **Rig budget:** 20 per hour, max 20

    ### ark-generalist
    - **Engine:** agy --model gemini-3.1-pro-low --permissions bypass
    - **Rig budget:** 30 per hour, max 20

    No `--sandbox`: agy's sandbox confines child writes to the CWD.

    ## Roster

    Members are `###` blocks. Names are addresses on the a8s network.

    - **Do not A/B member tiers here.** This roster is an instrument.

    ### Mira
    - **Rig:** ark-lead
    - **Leader:** yes
    - **Cell:** leadership
    - **Knowledge:** on
    - **Role:** Roster lead — holds the mission and routes every question

    ### Nora
    - **Rig:** ark-generalist
    - **Cell:** product
    - **Lead:** Mira
    - **Knowledge:** on
    - **Role:** Product manager

    ### Tess
    - **Rig:** ark-generalist
    - **Cell:** product
    - **Lead:** Nora
    - **Knowledge:** on
    - **Role:** Documentation

    ### Silas
    - **Rig:** ark-eng-claude
    - **Cell:** build
    - **Lead:** Mira
    - **Knowledge:** on
    - **Role:** Lead engineer

    ### Juno
    - **Rig:** ark-eng-cursor
    - **Cell:** build
    - **Lead:** Silas
    - **Knowledge:** on
    - **Role:** Engineer

    ## Rituals

    ### weekly-review
    - **When:** weekly mon 09:00
    - **To:** Nora

    Run the GTD weekly pass over the backlog.

    ### mission-review
    - **When:** on idle
    - **To:** Mira
"""


class TestAcceptanceExample:
    """The design's §4 acceptance test: six files collapse to one, and nothing
    load-bearing is lost."""

    def test_it_loads_clean(self, node, trusted):
        write(node, AR3)
        book = runbook.load_for_root(node, node=trusted)
        assert book.name == "AR3"
        assert book.chain == ["triforce", "ark-suite", "r4t.md"]
        assert [m.name for m in book.roster.members] == [
            "Mira", "Nora", "Tess", "Silas", "Juno"
        ]
        assert book.roster.leader().name == "Mira"
        assert sorted(book.cells) == ["build", "leadership", "product"]
        assert sorted(book.rigs) == [
            "ark-eng-claude", "ark-eng-cursor", "ark-generalist", "ark-lead"
        ]
        assert sorted(book.rituals) == ["mission-review", "weekly-review"]
        assert not [m.name for m in book.roster.members if m.errors]
        assert not [c.name for c in book.cells.values() if c.errors]
        assert not [r.name for r in book.rituals.values() if r.errors]
        assert not [n for n, r in book.rigs.items() if r.error]
        assert [w for w in book.warnings if "does not run them" not in w] == []

    def test_the_notes_became_prose(self, node):
        write(node, AR3)
        section = runbook.load_for_root(node).sections["Rigs"]
        assert "Widened from the preset's" in section.block("ark-lead").prose

    def test_the_org_config_moved_into_frontmatter(self, node):
        from org import load_org

        write(node, AR3)
        org = load_org(node)
        assert org.workplace == (node / "../..").resolve()
        assert org.comms == "open" and org.egress is True

    def test_every_member_resolves_a_rig_with_no_machine_config(
        self, node, tmp_path, trusted
    ):
        write(node, AR3)
        book = runbook.load_for_root(node, node=trusted)
        config = load_rig_config(tmp_path / "absent.json")
        for member in book.roster.members:
            rig, err, _pinned = config.rig_for(member)
            assert err is None, f"{member.name}: {err}"
            assert rig.preset in ("claude", "cursor", "agy")

    def test_it_round_trips_through_resolved(self, node):
        """A resolved runbook is a plain runbook: no `extends:` left, no
        substitution left, and feeding it back gives the same bytes."""
        write(node, AR3)
        first = runbook.render(runbook.load_for_root(node))
        assert "extends:" not in first
        (node / "again").mkdir()
        (node / "again" / runbook.RUNBOOK_NAME).write_text(first, encoding="utf-8")
        assert runbook.render(runbook.load_for_root(node / "again")) == first

    def test_triforce_round_trips_through_resolved(self, node):
        text = runbook.render(load_runbook(TRIFORCE))
        (node / runbook.RUNBOOK_NAME).write_text(text, encoding="utf-8")
        assert runbook.render(runbook.load_for_root(node)) == text


class TestOptionsSheet:
    """`docs/r4t-runbook.md` is the exhaustive key reference. A field the
    parser accepts and the page never names is undocumented; a field the page
    names and the parser rejects is a lie. One equality holds both directions,
    so adding a key to a block set fails here until the sheet grows a row."""

    SHEET = Path(__file__).resolve().parents[3] / "docs" / "r4t-runbook.md"

    def documented(self, heading: str) -> set[str]:
        """Keys in the first table under `## <heading>`, normalized the way a
        block's own bullets are."""
        parts = re.split(rf"^## {re.escape(heading)}$", self.SHEET.read_text(encoding="utf-8"), flags=re.M)
        assert len(parts) == 2, f"the options sheet has no `## {heading}` section"
        keys = set()
        for line in re.split(r"^## ", parts[1], flags=re.M)[0].splitlines():
            if not line.startswith("|"):
                continue
            cell = re.match(r"^`([^`]+)`", line.split("|")[1].strip())
            if cell:
                keys.add(runbook._normalize_key(cell.group(1).rstrip(":")))
        return keys

    @pytest.mark.parametrize(
        "heading,accepted",
        [
            ("Member fields", runbook.MEMBER_KEYS),
            ("Cell fields", runbook.CELL_KEYS),
            ("Rig fields", runbook.RIG_KEYS),
            ("Ritual fields", runbook.RITUAL_KEYS),
            (
                "Frontmatter",
                {runbook._normalize_key(k) for k in runbook.FRONTMATTER_KEYS},
            ),
        ],
    )
    def test_it_documents_every_accepted_field_and_no_others(self, heading, accepted):
        assert self.documented(heading) == accepted

    def test_it_carries_no_frontmatter(self):
        """A deep reference page must not become an installed skill: the
        `docs/*.md` symlinked by `install.sh --skills` are exactly those whose
        first line is `---`."""
        assert not self.SHEET.read_text(encoding="utf-8").startswith("---")
