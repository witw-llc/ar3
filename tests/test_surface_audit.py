"""Surface accounting — unit tests for tools/surface-audit.py.

Every test builds a miniature suite in tmp_path: one app, one docs page, one
tests directory. Nothing here reads the real tree, so the numbers the tool
reports on ar3 can move without moving these assertions.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "surface-audit.py"
_SPEC = importlib.util.spec_from_file_location("surface_audit", _TOOL)
audit = importlib.util.module_from_spec(_SPEC)
sys.modules["surface_audit"] = audit
_SPEC.loader.exec_module(audit)


CLI = '''
import argparse

DEMO_KEYS = (
    "alpha",
    "beta",
    "gamma",
)

DEFERRED_DEMO_KEYS = {"omega": "not in this release"}

GONE_DEMO_KEYS = {"ancient": "removed"}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("solid", help="A verb that is wired end to end")
    sub.add_parser("later", help="Print the ledger. Deferred until #4242")
    sub.add_parser("orphan", help="A verb nobody tests and nobody documents")
    nested = sub.add_parser("group", help="A verb with verbs under it")
    inner = nested.add_subparsers(dest="action")
    inner.add_parser("solid", help="Same word, different place")
'''

TESTS = '''
def test_solid():
    assert run(["solid"]) == 0


def test_group_solid():
    assert run(["group", "solid"]) == 0


def test_alpha():
    assert config("alpha") is not None
'''

DOCS = """
# demo

Run `demo solid` to do the thing, and `demo group solid` for the nested one.
The `alpha` key is the one you set.
"""

# The same two words, never together: one test types `group`, an unrelated one
# types `solid`. Both verbs exist on their own, so a flat set of every string
# in the suite holds both words and nothing typed `group solid`.
SPLIT_TESTS = '''
def test_group():
    assert run(["group"]) == 0


def test_solid():
    assert run(["solid"]) == 0


def test_alpha():
    assert config("alpha") is not None
'''

SPLIT_DOCS = """
# demo

Run `demo group` to reach the group, and `demo solid` for the flat verb.
The `alpha` key is the one you set.
"""


ALLOW_ALL = """cli:demo:orphan  # no test names it yet
config:demo:beta  # nothing reads it yet
config:demo:gamma  # nothing reads it yet
"""


@pytest.fixture
def tree(tmp_path):
    """A one-app suite with a wired, a deferred, and an unaccounted item."""
    app = tmp_path / "apps" / "demo"
    (app / "tests").mkdir(parents=True)
    (app / "cli.py").write_text(CLI, encoding="utf-8")
    (app / "tests" / "test_demo.py").write_text(TESTS, encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "demo.md").write_text(DOCS, encoding="utf-8")
    definitions = tmp_path / "apps" / "a8s" / "definitions"
    definitions.mkdir(parents=True)
    (definitions / "solid.json").write_text('{"invoke": "x"}', encoding="utf-8")
    a8s_tests = tmp_path / "apps" / "a8s" / "tests"
    a8s_tests.mkdir()
    (a8s_tests / "test_defs.py").write_text(
        'def test_load():\n    assert load("solid")\n', encoding="utf-8"
    )
    return tmp_path


def verdicts(root):
    return {item.key: verdict for item, verdict in audit.audit(root)}


def run(root, *extra):
    allow = root / "allow.txt"
    argv = ["--root", str(root), "--allow", str(allow), *extra]
    return audit.main(argv)


# ---------- classification ----------


def test_a_verb_a_test_names_and_a_doc_shows_is_wired(tree):
    assert verdicts(tree)["cli:demo:solid"] == "wired"


def test_a_verb_whose_help_cites_an_issue_is_deferred(tree):
    assert verdicts(tree)["cli:demo:later"] == "deferred"


def test_a_verb_with_neither_is_unaccounted(tree):
    assert verdicts(tree)["cli:demo:orphan"] == "unaccounted"


def test_the_word_deferred_marks_a_verb_too(tree):
    text = (tree / "apps" / "demo" / "cli.py").read_text(encoding="utf-8")
    (tree / "apps" / "demo" / "cli.py").write_text(
        text.replace("Deferred until #4242", "Deferred"), encoding="utf-8"
    )
    assert verdicts(tree)["cli:demo:later"] == "deferred"


def test_a_nested_verb_keeps_its_parent_in_the_name(tree):
    found = verdicts(tree)
    assert found["cli:demo:group solid"] == "wired"
    assert "cli:demo:solid" in found


def test_one_argv_naming_both_words_is_the_wiring(tree):
    """The positive control: `["group", "solid"]` in one call is evidence."""
    assert verdicts(tree)["cli:demo:group solid"] == "wired"


def test_two_tests_naming_a_word_each_are_not_the_wiring(tree):
    """Deleting `group solid` leaves both tests passing, so neither wires it."""
    (tree / "apps" / "demo" / "tests" / "test_demo.py").write_text(
        SPLIT_TESTS, encoding="utf-8"
    )
    found = verdicts(tree)
    assert found["cli:demo:group solid"] == "unaccounted"
    assert found["cli:demo:group"] == "wired"
    assert found["cli:demo:solid"] == "wired"


def test_two_docs_spans_naming_a_word_each_do_not_document_the_command(tree):
    """A page that shows `demo group` and `demo solid` never shows the nested one."""
    (tree / "docs" / "demo.md").write_text(SPLIT_DOCS, encoding="utf-8")
    found = verdicts(tree)
    assert found["cli:demo:group solid"] == "unaccounted"
    assert found["cli:demo:solid"] == "wired"


def test_a_fenced_command_line_documents_the_whole_command(tree):
    """The positive control for docs: both words on one copyable line."""
    (tree / "docs" / "demo.md").write_text(
        "# demo\n\n```bash\ndemo group solid\n```\n\nThe `alpha` key.\n",
        encoding="utf-8",
    )
    assert verdicts(tree)["cli:demo:group solid"] == "wired"


def test_a_key_table_is_read_and_a_gone_table_is_not(tree):
    found = verdicts(tree)
    assert found["config:demo:alpha"] == "wired"
    assert found["config:demo:beta"] == "unaccounted"
    assert found["config:demo:omega"] == "deferred"
    assert "config:demo:ancient" not in found


def test_a_bundled_definition_is_a_surface(tree):
    assert verdicts(tree)["runbook:a8s:solid"] == "wired"


def test_a_docstring_is_not_evidence_of_wiring(tree):
    """The verb named only in a test's prose stays unaccounted."""
    tests = tree / "apps" / "demo" / "tests" / "test_demo.py"
    tests.write_text('"""Covers orphan."""\n\ndef test_x():\n    pass\n', encoding="utf-8")
    assert verdicts(tree)["cli:demo:orphan"] == "unaccounted"


def test_docs_alone_is_not_wired(tree):
    """`beta` reaches the docs but no test, so it is not wired."""
    (tree / "docs" / "demo.md").write_text("`beta` is a key.\n", encoding="utf-8")
    assert verdicts(tree)["config:demo:beta"] == "unaccounted"


# ---------- exit codes and the allowlist ----------


def test_an_unaccounted_item_fails_the_check(tree):
    (tree / "allow.txt").write_text("", encoding="utf-8")
    assert run(tree) == 1


def test_the_allowlist_suppresses_what_it_names(tree, capsys):
    (tree / "allow.txt").write_text(ALLOW_ALL, encoding="utf-8")
    assert run(tree) == 0
    assert "(allowed)" in capsys.readouterr().out


def test_an_allowlist_line_without_a_reason_fails(tree, capsys):
    (tree / "allow.txt").write_text("cli:demo:orphan\nconfig:demo:beta  # why\n", encoding="utf-8")
    assert run(tree) == 1
    assert "no reason given" in capsys.readouterr().out


def test_an_allowlist_line_that_is_no_longer_needed_is_reported(tree, capsys):
    extra = "cli:demo:solid  # this one is wired already\n"
    (tree / "allow.txt").write_text(ALLOW_ALL + extra, encoding="utf-8")
    assert run(tree) == 0
    assert "cli:demo:solid is accounted for now" in capsys.readouterr().out


def test_comments_and_blank_lines_are_not_entries(tree):
    (tree / "allow.txt").write_text("# a header\n\n", encoding="utf-8")
    assert run(tree) == 1


# ---------- reports ----------


def test_json_carries_every_item_and_the_counts(tree, capsys):
    (tree / "allow.txt").write_text("cli:demo:orphan  # later\n", encoding="utf-8")
    run(tree, "--json")
    payload = json.loads(capsys.readouterr().out)
    names = {row["name"] for row in payload["items"]}
    assert {"solid", "later", "orphan"} <= names
    assert payload["counts"]["cli"]["deferred"] == 1
    assert "cli:demo:orphan" not in payload["blocking"]
    assert all("origin" in row for row in payload["items"])


def test_the_table_totals_every_surface(tree, capsys):
    (tree / "allow.txt").write_text("", encoding="utf-8")
    run(tree)
    out = capsys.readouterr().out
    assert "cli" in out and "config" in out and "runbook" in out
    assert "surfaces:" in out


# ---------- the real tree ----------


def test_the_repo_itself_is_accounted_for():
    """The seeded allowlist keeps the suite green; a new surface turns it red."""
    assert audit.main([]) == 0


def test_every_line_of_the_shipped_allowlist_carries_a_reason():
    allowed, malformed = audit.read_allowlist(
        Path(__file__).resolve().parents[1] / "tools" / "surface-audit.allow"
    )
    assert allowed
    assert malformed == []
