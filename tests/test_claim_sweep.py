"""Claim sweep — unit tests for tools/claim-sweep.py.

Each test builds a miniature surface tree in tmp_path, so nothing here depends
on what the real docs happen to say today.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "claim-sweep.py"
_SPEC = importlib.util.spec_from_file_location("claim_sweep", _TOOL)
sweeper = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sweeper)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tree(tmp_path):
    """A root with the three scanned surfaces and nothing to report."""
    root = tmp_path / "surface"
    (root / "docs").mkdir(parents=True)
    (root / "guide").mkdir()
    (root / "README.md").write_text("# ar3\n\nFour apps that ship together.\n", encoding="utf-8")
    (root / "docs" / "a8s.md").write_text("# a8s\n\nA message router.\n", encoding="utf-8")
    (root / "guide" / "README.md").write_text("# The Ark Raising\n\nA build-along.\n", encoding="utf-8")
    return root


def write(tree, relative, body):
    path = tree / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def rules(findings):
    return [(relative, rule, matched) for relative, _, rule, matched in findings]


def test_clean_tree_reports_nothing(tree):
    assert sweeper.sweep(tree) == []


def test_nominal_cadence_claim_is_flagged(tree):
    write(tree, "docs/a8s.md", "The daemon wakes every 30 seconds.\n")
    assert rules(sweeper.sweep(tree)) == [("docs/a8s.md", "cadence", "every 30 seconds")]


def test_measured_marker_clears_the_same_sentence(tree):
    write(tree, "docs/a8s.md", "The daemon wakes every 30 seconds, measured on 2026-08-30.\n")
    assert sweeper.sweep(tree) == []


def test_hedge_clears_the_same_sentence(tree):
    write(tree, "docs/a8s.md", "The daemon wakes about every 30 seconds.\n")
    assert sweeper.sweep(tree) == []


def test_citation_link_clears_the_same_sentence(tree):
    write(tree, "docs/a8s.md", "It wakes every 30 seconds ([the run](runs/2026-08-30.md)).\n")
    assert sweeper.sweep(tree) == []


def test_marker_in_a_neighbouring_sentence_does_not_clear_the_claim(tree):
    write(tree, "docs/a8s.md", "We measured the router. The daemon wakes every 30 seconds.\n")
    assert rules(sweeper.sweep(tree)) == [("docs/a8s.md", "cadence", "every 30 seconds")]


@pytest.mark.parametrize(
    "claim, matched",
    [
        ("Mail lands within 5 seconds.", "within 5 seconds"),
        ("A turn finishes in under 3 minutes.", "in under 3"),
        ("The router adds 40ms latency.", "40ms latency"),
    ],
)
def test_every_cadence_form_is_flagged(tree, claim, matched):
    write(tree, "docs/a8s.md", claim + "\n")
    assert rules(sweeper.sweep(tree)) == [("docs/a8s.md", "cadence", matched)]


def test_a_number_inside_code_is_configuration_not_a_claim(tree):
    write(tree, "docs/a8s.md", "Set the interval:\n\n```json\n{\"wake\": \"every 30 seconds\"}\n```\n")
    assert sweeper.sweep(tree) == []


def test_a_flag_name_in_backticks_is_not_a_reliability_claim(tree):
    write(tree, "docs/a8s.md", "Aider takes `--yes-always` to skip the prompt.\n")
    assert sweeper.sweep(tree) == []


def test_absolute_reliability_word_is_flagged(tree):
    write(tree, "docs/a8s.md", "Delivery is guaranteed.\n")
    assert rules(sweeper.sweep(tree)) == [("docs/a8s.md", "absolute", "guaranteed")]


def test_denying_the_guarantee_is_not_a_claim(tree):
    write(tree, "docs/a8s.md", "Ordering is environment-specific rather than guaranteed.\n")
    assert sweeper.sweep(tree) == []


def test_always_needs_a_reliability_predicate(tree):
    write(tree, "docs/a8s.md", "The member is always the second field.\n")
    assert sweeper.sweep(tree) == []


def test_always_works_is_flagged(tree):
    write(tree, "docs/a8s.md", "The wake path always works.\n")
    assert rules(sweeper.sweep(tree)) == [("docs/a8s.md", "absolute", "always works")]


def test_banned_phrase_in_readme_is_flagged(tree):
    write(tree, "README.md", "ar3 is a framework to orchestrate a team of agents.\n")
    found = rules(sweeper.sweep(tree))
    assert ("README.md", "banned", "orchestrate a team") in found
    assert ("README.md", "banned", "framework") in found


def test_a_dash_in_readme_is_flagged_and_in_docs_is_not(tree):
    (tree / "README.md").write_text("One file \u2014 a team shows up.\n", encoding="utf-8")
    (tree / "docs" / "x.md").write_text("A note \u2013 with a dash.\n", encoding="utf-8")
    found = rules(sweeper.sweep(tree))
    assert ("README.md", "dash", "\u2014") in found
    assert all(not (rel == "docs/x.md" and rule == "dash") for rel, rule, _ in found)


def test_banned_phrase_in_docs_is_not_flagged(tree):
    write(tree, "docs/a8s.md", "LangChain is a framework; a8s is not one.\n")
    assert sweeper.sweep(tree) == []


def test_the_guide_readme_carries_the_ban_too(tree):
    write(tree, "guide/README.md", "Build a no-code dashboard.\n")
    assert {rule for _, rule, _ in rules(sweeper.sweep(tree))} == {"banned"}


def test_the_allowlist_suppresses_a_known_good_line(tree):
    write(tree, "docs/a8s.md", "Delivery is guaranteed.\n")
    write(tree, "tools/claim-sweep.allow", "# reviewed\n^docs/a8s\\.md: Delivery is guaranteed\\.\n")
    assert sweeper.sweep(tree) == []


def test_the_allowlist_is_scoped_by_the_path_it_names(tree):
    write(tree, "docs/a8s.md", "Delivery is guaranteed.\n")
    write(tree, "docs/r4t.md", "Delivery is guaranteed.\n")
    write(
        tree,
        "tools/claim-sweep.allow",
        "# reviewed\n^docs/a8s\\.md: Delivery is guaranteed\\.\n",
    )
    assert rules(sweeper.sweep(tree)) == [("docs/r4t.md", "absolute", "guaranteed")]


def test_a_pattern_with_no_reason_suppresses_nothing_and_fails_the_run(tree, capsys):
    """`.*` on its own would silence every surface. It silences none."""
    write(tree, "docs/a8s.md", "Delivery is guaranteed.\n")
    write(tree, "tools/claim-sweep.allow", ".*\n")
    assert rules(sweeper.sweep(tree)) == [("docs/a8s.md", "absolute", "guaranteed")]
    assert sweeper.main([str(tree)]) == 1
    captured = capsys.readouterr()
    assert "tools/claim-sweep.allow:1: no reason given for .*" in captured.out
    assert "1 allowlist entry with no reason" in captured.err


def test_the_files_own_header_is_not_a_reason_for_the_first_entry(tree, capsys):
    """A blank line ends a reason, so the header cannot vouch for a pattern."""
    write(tree, "docs/a8s.md", "Delivery is guaranteed.\n")
    write(tree, "tools/claim-sweep.allow", "# how this file works\n\n.*\n")
    assert rules(sweeper.sweep(tree)) == [("docs/a8s.md", "absolute", "guaranteed")]
    assert sweeper.main([str(tree)]) == 1
    assert "tools/claim-sweep.allow:3: no reason given" in capsys.readouterr().out


def test_a_reason_above_the_pattern_lets_it_through(tree):
    """The positive control for the grammar the shipped allowlist uses."""
    write(tree, "docs/a8s.md", "Delivery is guaranteed.\n")
    write(
        tree,
        "tools/claim-sweep.allow",
        "# The word names a contract in code, not a rate.\n"
        "# Read the call site on 2026-09-03.\n"
        "^docs/a8s\\.md: Delivery is guaranteed\\.\n",
    )
    assert sweeper.sweep(tree) == []
    assert sweeper.main([str(tree)]) == 0


def test_a_reason_does_not_carry_over_to_the_next_pattern(tree, capsys):
    """Reviewer repro: a narrow entry's own reason must not vouch for a
    second pattern stacked directly under it with no blank line and no
    comment of its own. `.*` here would silence every surface if it did."""
    write(tree, "docs/a8s.md", "Delivery is guaranteed.\n")
    write(
        tree,
        "tools/claim-sweep.allow",
        "# reason for narrow entry\n"
        "^docs/other\\.md: nothing here\n"
        ".*\n",
    )
    assert rules(sweeper.sweep(tree)) == [("docs/a8s.md", "absolute", "guaranteed")]
    assert sweeper.main([str(tree)]) == 1
    captured = capsys.readouterr()
    assert "tools/claim-sweep.allow:3: no reason given for .*" in captured.out
    assert "docs/a8s.md:1: absolute — guaranteed" in captured.out


def test_every_entry_in_the_shipped_allowlist_carries_a_reason():
    patterns, reasonless = sweeper.load_allowlist(REPO_ROOT)
    assert patterns
    assert reasonless == []


def test_a_comment_only_allowlist_suppresses_nothing(tree):
    write(tree, "docs/a8s.md", "Delivery is guaranteed.\n")
    write(tree, "tools/claim-sweep.allow", "# guaranteed\n\n")
    assert rules(sweeper.sweep(tree)) == [("docs/a8s.md", "absolute", "guaranteed")]


def test_the_guides_templates_are_scanned(tree):
    write(tree, "guide/templates/01-solo-claude/AGENTS.md", "Replies arrive within 5 seconds.\n")
    assert rules(sweeper.sweep(tree)) == [
        ("guide/templates/01-solo-claude/AGENTS.md", "cadence", "within 5 seconds")
    ]


def test_the_reported_line_is_the_line_the_phrase_sits_on(tree):
    write(tree, "docs/a8s.md", "# a8s\n\nA router.\n\nThe daemon wakes\nevery 30 seconds.\n")
    assert [number for _, number, _, _ in sweeper.sweep(tree)] == [6]


def test_findings_exit_nonzero_and_a_clean_tree_exits_zero(tree, capsys):
    assert sweeper.main([str(tree)]) == 0
    assert "clean across" in capsys.readouterr().out
    write(tree, "docs/a8s.md", "Delivery is guaranteed.\n")
    assert sweeper.main([str(tree)]) == 1
    captured = capsys.readouterr()
    assert "docs/a8s.md:1: absolute — guaranteed" in captured.out
    assert "1 unmeasured claim" in captured.err


def test_the_repo_tree_carries_no_unmeasured_claim():
    assert sweeper.sweep(REPO_ROOT) == []
