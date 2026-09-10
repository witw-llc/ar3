"""Wiki gardening checks — unit tests for tools/wiki-gardener.py.

Each test builds a miniature wiki in tmp_path, so nothing here reads the real
wiki checkout or the network.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "wiki-gardener.py"
_SPEC = importlib.util.spec_from_file_location("wiki_gardener", _TOOL)
gardener = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gardener)

HEADER = "`Category: {category}` · `State: {state}`\n\n# {title}\n\n"


def page(category="Reference", state="Validated", title="A page", body=""):
    return HEADER.format(category=category, state=state, title=title) + body + "\n"


@pytest.fixture
def wiki(tmp_path):
    """A clean wiki: a sidebar, one index page, and one leaf the index lists."""
    root = tmp_path / "wiki"
    root.mkdir()
    pages = {
        "_Sidebar": "**[Reference](Reference)**\n",
        "Reference": page(title="Reference", body="- [Positioning](Reference-Positioning)"),
        "Reference-Positioning": page(title="Positioning"),
    }
    for name, text in pages.items():
        (root / f"{name}.md").write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def write(wiki):
    """Add or replace a page in the fixture wiki."""
    def _write(name, text):
        (wiki / f"{name}.md").write_text(text, encoding="utf-8")
    return _write


LEDGER_COLUMNS = (
    "| Decision | Date | Ruling | Consequences | Evidence | Status |\n"
    "|---|---|---|---|---|---|\n"
)


def ledger_page(sections):
    """A decision ledger from [(domain, [(title, status), ...]), ...]."""
    body = []
    for domain, rows in sections:
        body.append(f"## {domain}\n\n" + LEDGER_COLUMNS)
        for title, status in rows:
            body.append(f"| {title} | 2026-09-10 | A ruling. | not recorded | #1 | {status} |\n")
        body.append("\n")
    return page(category="Decisions", title="Decisions", body="".join(body))


def kinds(root):
    defects, _, _ = gardener.garden(root)
    return [(d.page, d.kind) for d in defects]


def test_a_clean_wiki_reports_nothing(wiki):
    assert kinds(wiki) == []


def test_clean_wiki_exits_zero(wiki, capsys):
    assert gardener.main([str(wiki)]) == 0
    assert "no defects" in capsys.readouterr().out


def test_page_without_a_category_is_uncategorized(wiki, write):
    write("Reference-Loose", "# Loose\n\nBody.\n")
    write("Reference", page(title="Reference", body="- [Loose](Reference-Loose)"))
    assert ("Reference-Loose", "uncategorized") in kinds(wiki)


def test_a_category_outside_the_closed_set_is_a_defect(wiki, write):
    write("Reference-Loose", page(category="Miscellany", title="Loose"))
    write("Reference", page(title="Reference", body="- [Loose](Reference-Loose)"))
    defects, _, _ = gardener.garden(wiki)
    detail = next(d.detail for d in defects if d.page == "Reference-Loose")
    assert "Miscellany" in detail


def test_page_without_a_state_is_stateless(wiki, write):
    write("Reference-Loose", "`Category: Reference`\n\n# Loose\n")
    write("Reference", page(title="Reference", body="- [Loose](Reference-Loose)"))
    assert ("Reference-Loose", "stateless") in kinds(wiki)


def test_home_declares_neither_category_nor_state(wiki, write):
    write("_Sidebar", "**[Home](Home)**\n\n**[Reference](Reference)**\n")
    write("Home", "# ar3 — wiki\n\n- [Reference](Reference)\n")
    assert kinds(wiki) == []


def test_a_banner_with_a_reason_and_a_date_is_clean(wiki, write):
    write(
        "Reference-Positioning",
        page(
            title="Positioning",
            body="> **Out of date** — written against the 0.1.40 layout — 2026-08-06",
        ),
    )
    assert kinds(wiki) == []


def test_a_banner_without_a_date_is_unexplained(wiki, write):
    write(
        "Reference-Positioning",
        page(title="Positioning", body="> **Accuracy** — the throughput number was never taken"),
    )
    defects, _, _ = gardener.garden(wiki)
    detail = next(d.detail for d in defects if d.kind == "unexplained-flag")
    assert "no date" == detail.split("carries ")[1]


def test_a_bare_banner_has_neither_reason_nor_date(wiki, write):
    write("Reference-Positioning", page(title="Positioning", body="**Archive**"))
    defects, _, _ = gardener.garden(wiki)
    detail = next(d.detail for d in defects if d.kind == "unexplained-flag")
    assert detail.endswith("no reason and no date")


def test_a_banner_reason_may_run_onto_following_quoted_lines(wiki, write):
    write(
        "Reference-Positioning",
        page(
            title="Positioning",
            body="> **Move**\n> The page belongs under Plans, 2026-08-06.",
        ),
    )
    assert kinds(wiki) == []


def test_a_flag_name_below_the_first_section_is_not_a_banner(wiki, write):
    write(
        "Reference-Positioning",
        page(title="Positioning", body="## Notes\n\nThe **Merge** verb is r4t's."),
    )
    assert kinds(wiki) == []


def test_a_page_no_index_links_is_an_orphan(wiki, write):
    write("Reference-Stray", page(title="Stray"))
    assert ("Reference-Stray", "orphan") in kinds(wiki)


def test_a_third_hop_is_out_of_reach(wiki, write):
    write("Reference-Positioning", page(title="Positioning", body="- [Deep](Reference-Deep)"))
    write("Reference-Deep", page(title="Deep"))
    assert ("Reference-Deep", "orphan") in kinds(wiki)


def test_a_sidebar_entry_that_is_not_an_index_is_a_leaf(wiki, write):
    write("_Sidebar", "**[Reference](Reference)**\n- [Positioning](Reference-Positioning)\n")
    assert ("_Sidebar", "sidebar-leaf") in kinds(wiki)


def test_pluralized_index_names_are_accepted(wiki, write):
    write("_Sidebar", "**[Stories](Stories)**\n")
    write("Stories", page(category="Story", title="Stories", body="- [One](Story-01)"))
    write("Story-01", page(category="Story", title="One"))
    (wiki / "Reference.md").unlink()
    (wiki / "Reference-Positioning.md").unlink()
    assert kinds(wiki) == []


def test_a_cross_cutting_index_belongs_in_the_sidebar(wiki, write):
    write("_Sidebar", "**[Reference](Reference)**\n**[Attention](Attention)**\n")
    write("Attention", page(category="Playbook", title="Attention"))
    assert kinds(wiki) == []


def test_a_link_to_a_missing_page_is_dead(wiki, write):
    write("Reference-Positioning", page(title="Positioning", body="See [the plan](Plans-Gone)."))
    assert ("Reference-Positioning", "dead-link") in kinds(wiki)


def test_a_wiki_link_to_a_missing_page_is_dead(wiki, write):
    write("Reference-Positioning", page(title="Positioning", body="See [[Show|Plans-Gone]]."))
    assert ("Reference-Positioning", "dead-link") in kinds(wiki)


def test_a_link_carrying_a_path_is_dead_because_the_wiki_is_flat(wiki, write):
    write(
        "Reference-Positioning",
        page(title="Positioning", body="See [tell](../apps/a8s/docs/tell.md)."),
    )
    assert ("Reference-Positioning", "dead-link") in kinds(wiki)


def test_external_links_and_anchors_are_left_alone(wiki, write):
    write(
        "Reference-Positioning",
        page(title="Positioning", body="[up](#top) and [out](https://example.invalid/x)"),
    )
    assert kinds(wiki) == []


def test_a_wiki_link_written_with_spaces_resolves(wiki, write):
    write("Reference", page(title="Reference", body="- [[Reference Positioning]]"))
    assert kinds(wiki) == []


def test_a_link_to_an_attachment_resolves(wiki, write):
    (wiki / "Reference-Positioning.m4a").write_bytes(b"")
    write(
        "Reference-Positioning",
        page(title="Positioning", body="[audio](Reference-Positioning.m4a)"),
    )
    assert kinds(wiki) == []


def test_links_inside_fenced_blocks_are_ignored(wiki, write):
    write(
        "Reference-Positioning",
        page(title="Positioning", body="```\n[example](Plans-Gone)\n```"),
    )
    assert kinds(wiki) == []


def test_defects_exit_one_and_print_one_line_each(wiki, capsys, write):
    write("Reference-Stray", "# Stray\n")
    assert gardener.main([str(wiki)]) == 1
    out = capsys.readouterr().out
    assert "Reference-Stray: uncategorized — " in out
    assert "3 defects on 1 of 4 pages" in out


def test_json_output_is_machine_readable(wiki, capsys, write):
    import json

    write("Reference-Stray", page(title="Stray"))
    assert gardener.main([str(wiki), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["pages"] == 4
    assert report["defects"] == [
        {
            "page": "Reference-Stray",
            "defect": "orphan",
            "detail": "unreachable from _Sidebar within 2 link hops",
        }
    ]


def test_a_missing_sidebar_is_reported_rather_than_crashing(wiki):
    (wiki / "_Sidebar.md").unlink()
    assert ("_Sidebar", "orphan") in kinds(wiki)


def test_a_path_that_is_not_a_directory_is_a_usage_error(tmp_path):
    with pytest.raises(SystemExit) as exit_info:
        gardener.main([str(tmp_path / "absent")])
    assert exit_info.value.code == 2


@pytest.fixture
def ledger(wiki, write):
    """Put a decision ledger in the fixture wiki and reach it from the sidebar."""
    write("_Sidebar", "**[Reference](Reference)**\n**[Decisions](Decisions)**\n")

    def _ledger(sections):
        write("Decisions", ledger_page(sections))
        return wiki
    return _ledger


def test_a_consolidated_ledger_reports_nothing(ledger):
    root = ledger([
        ("Comms & protocol", [("tell is the only verb", "standing")]),
        ("Process & lab", [("Trust but grep", "standing")]),
    ])
    assert kinds(root) == []


def test_the_row_count_is_reported_even_when_the_ledger_is_clean(ledger, capsys):
    root = ledger([("Process & lab", [("Trust but grep", "standing")])])
    assert gardener.main([str(root)]) == 0
    assert "Decisions: 1 live rows against a ceiling of 120" in capsys.readouterr().out


def test_a_ledger_over_the_ceiling_is_a_defect(ledger):
    rows = [(f"Ruling number {n}", "standing") for n in range(gardener.LEDGER_CEILING + 1)]
    root = ledger([("Process & lab", rows)])
    assert ("Decisions", "ledger-oversize") in kinds(root)


def test_the_ceiling_counts_live_rows_only(ledger):
    rows = [(f"Ruling number {n}", "standing") for n in range(gardener.LEDGER_CEILING)]
    rows.append(("One row too many", "superseded by *Ruling number 1*"))
    root = ledger([("Process & lab", rows)])
    assert ("Decisions", "ledger-oversize") not in kinds(root)


def test_a_superseded_row_belongs_in_the_archive(ledger):
    root = ledger([("Process & lab", [
        ("Issues plus feature branches", "superseded by *Batch to a version branch*"),
        ("Batch to a version branch", "standing"),
    ])])
    defects, _, _ = gardener.garden(root)
    unarchived = [d for d in defects if d.kind == "unarchived-row"]
    assert len(unarchived) == 1
    assert "Issues plus feature branches" in unarchived[0].detail
    assert gardener.LEDGER_ARCHIVE in unarchived[0].detail


def test_two_live_rows_on_one_surface_are_an_unconsolidated_pair(ledger):
    root = ledger([("Product & positioning", [
        ("The product is ar3", "standing"),
        ("The product is written AR3 in prose", "standing"),
    ])])
    defects, _, _ = gardener.garden(root)
    pair = next(d for d in defects if d.kind == "unconsolidated-pair")
    assert "The product is ar3" in pair.detail
    assert "The product is written AR3 in prose" in pair.detail
    assert "Product & positioning" in pair.detail


def test_a_repeated_title_is_a_pair_with_itself(ledger):
    root = ledger([("Memory & knowledge", [
        ("Rank-proportional packing", "standing"),
        ("Rank-proportional packing", "standing"),
    ])])
    assert [k for k in kinds(root) if k[1] == "unconsolidated-pair"] == [
        ("Decisions", "unconsolidated-pair")
    ]


def test_three_rows_on_one_surface_pair_against_the_first(ledger):
    root = ledger([("Memory & knowledge", [
        ("The priming query is bare", "standing"),
        ("The priming query is short", "standing"),
        ("The priming query is newest", "standing"),
    ])])
    pairs = [d for d in kinds(root) if d[1] == "unconsolidated-pair"]
    assert len(pairs) == 2


def test_markup_in_a_title_does_not_hide_a_surface(ledger):
    root = ledger([("Comms & protocol", [
        ("`tell` is the only verb", "standing"),
        ("tell is the verb that never blocks", "standing"),
    ])])
    assert [k for k in kinds(root) if k[1] == "unconsolidated-pair"] == [
        ("Decisions", "unconsolidated-pair")
    ]


def test_titles_that_agree_on_fewer_words_are_different_surfaces(ledger):
    root = ledger([("Product & positioning", [
        ("The product is ar3", "standing"),
        ("The product ships from one URL", "standing"),
        ("The reader is the audience", "standing"),
    ])])
    assert kinds(root) == []


def test_one_surface_in_two_domains_is_not_a_pair(ledger):
    root = ledger([
        ("Memory & knowledge", [("Members write only private memory", "standing")]),
        ("Architecture & isolation", [("Members write only private stores", "standing")]),
    ])
    assert kinds(root) == []


def test_a_superseded_row_never_pairs_with_its_successor(ledger):
    root = ledger([("Product & positioning", [
        ("The product is The Ark", "superseded by *The product is ar3*"),
        ("The product is ar3", "standing"),
    ])])
    assert [k for k in kinds(root) if k[1] == "unconsolidated-pair"] == []


def test_titles_that_carry_separators_are_read_up_to_the_separator():
    text = ledger_page([("Comms & protocol", [
        ("tell — the only verb", "standing"),
        ("tell — never blocks", "standing"),
        ("Obligation: stops at the wall", "standing"),
    ])])
    defects, summary = gardener.check_ledger(text)
    assert summary["surface"] == "the words before the first dash or colon"
    pair = next(d for d in defects if d.kind == "unconsolidated-pair")
    assert '"tell"' in pair.detail


def test_titles_without_separators_are_read_as_their_first_words():
    text = ledger_page([("Comms & protocol", [
        ("tell is the only verb", "standing"),
        ("tell is the one that never blocks", "standing"),
    ])])
    defects, summary = gardener.check_ledger(text)
    assert summary["surface"].startswith(f"the first {gardener.SURFACE_WORDS} words")
    assert [d.kind for d in defects] == ["unconsolidated-pair"]


def test_the_json_report_carries_the_ledger_summary(ledger, capsys):
    import json

    root = ledger([("Process & lab", [("Trust but grep", "standing")])])
    assert gardener.main([str(root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ledger"]["rows"] == 1
    assert report["ledger"]["ceiling"] == gardener.LEDGER_CEILING


def test_a_wiki_without_a_ledger_reports_no_summary(wiki, capsys):
    assert gardener.main([str(wiki), "--json"]) == 0
    import json

    assert "ledger" not in json.loads(capsys.readouterr().out)
