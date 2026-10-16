"""A compile replaces the entries it folds, and a scheduled compile runs when due."""
import os
import time

import cli
import engine


def llm(monkeypatch, calls=None, reply="Synthetic compiled reference about irrigation."):
    calls = [] if calls is None else calls
    monkeypatch.setattr(engine, "_call_llm", lambda prompt, **kwargs: calls.append(prompt) or reply)
    return calls


def status(node_id):
    return engine._parse_frontmatter(engine.get(node_id)).get("status", "active")


def entries(tag, n, extra=(), start=0):
    return [engine.store_entry(f"Synthetic {tag} procedure {i}", f"Synthetic {tag} irrigation step {i}.",
                               tags=[tag, *extra]) for i in range(start, start + n)]


def test_page_is_active_and_supersedes_every_source(store, monkeypatch):
    llm(monkeypatch)
    sources = entries("garden", 3, extra=("water",))
    page = engine.compile_tag("garden")
    meta = engine._parse_frontmatter(engine.get(page))
    assert meta["status"] == "active"
    assert meta["tags"][:3] == ["garden", "compiled", "water"]
    for sid in sources:
        assert status(sid) == "superseded"
        assert engine._parse_frontmatter(engine.get(sid))["superseded_by"] == page
    hits = [h["id"] for h in engine.search("irrigation", limit=10)]
    assert page in hits and not set(sources) & set(hits)


def test_a_tag_that_contains_the_name_is_not_folded(store, monkeypatch):
    llm(monkeypatch)
    entries("dev", 3)
    other = entries("devops", 1)
    engine.compile_tag("dev")
    assert status(other[0]) == "active"


def test_a_page_folds_only_into_its_own_tag(store, monkeypatch):
    calls = llm(monkeypatch)
    entries("garden", 3, extra=("water",))
    page = engine.compile_tag("garden")
    entries("water", 3)
    engine.compile_tag("water")
    assert status(page) == "active"
    assert page not in calls[-1]
    later = entries("garden", 2, start=3)
    again = engine.compile_tag("garden")
    assert status(page) == "superseded"
    assert all(status(sid) == "superseded" for sid in later)
    assert page in calls[-1] and again


def test_failed_model_call_leaves_sources_active(store, monkeypatch):
    llm(monkeypatch, reply="")
    sources = entries("garden", 3)
    assert engine.compile_tag("garden") is None
    assert all(status(sid) == "active" for sid in sources)


def test_due_compiles_the_largest_ready_tag_once_per_interval(store, monkeypatch):
    llm(monkeypatch)
    small = entries("water", 5)
    large = entries("garden", 6)
    state, page = engine.compile_due()
    assert state == "compiled" and page
    assert all(status(sid) == "superseded" for sid in large)
    assert all(status(sid) == "active" for sid in small)
    assert engine.compile_due() == ("not-due", None)


def test_due_below_threshold_does_nothing_and_waits(store, monkeypatch):
    calls = llm(monkeypatch)
    entries("garden", 4)
    assert engine.compile_due() == ("nothing", None)
    assert calls == []
    assert engine.compile_due() == ("not-due", None)


def test_due_runs_again_after_the_interval(store, monkeypatch):
    llm(monkeypatch)
    entries("garden", 4)
    engine.compile_due()
    old = time.time() - (engine.COMPILE_DUE_HOURS + 1) * 3600
    os.utime(store / ".compile-due", (old, old))
    entries("garden", 1, start=4)
    assert engine.compile_due()[0] == "compiled"


def test_a_failed_due_pass_waits_out_the_interval(store, monkeypatch):
    llm(monkeypatch, reply="")
    sources = entries("garden", 5)
    assert engine.compile_due() == ("failed", None)
    assert engine.compile_due() == ("not-due", None)
    assert all(status(sid) == "active" for sid in sources)


def test_cli_takes_a_tag_or_due_alone(store, monkeypatch, capsys):
    llm(monkeypatch)
    assert cli.main(["compile"]) == 2
    assert cli.main(["compile", "garden", "--due"]) == 2
    assert cli.main(["compile", "--due", "--dry-run"]) == 2
    # Nothing ready needs no model, so an unconfigured store passes quietly.
    assert cli.main(["compile", "--due"]) == 0
    assert "compile: nothing" in capsys.readouterr().out


def test_cli_due_with_a_ready_tag_and_no_model_says_so(store, capsys):
    entries("garden", 5)
    assert cli.main(["compile", "--due"]) == 1
    assert "compile" in capsys.readouterr().err


def test_a_task_entry_is_neither_folded_nor_counted(store, monkeypatch):
    calls = llm(monkeypatch)
    entries("garden", 4)
    task = engine.store_entry("Synthetic garden run", "Synthetic garden run passed today.",
                              tags=["garden"], valid_to="2099-01-01")
    assert engine.compile_due() == ("nothing", None)
    entries("garden", 1, start=4)
    os.utime(store / ".compile-due", (0, 0))
    assert engine.compile_due()[0] == "compiled"
    assert task not in calls[-1] and status(task) == "active"


def test_after_a_failed_tag_the_next_pass_tries_another(store, monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "_call_llm",
                        lambda prompt, **kwargs: calls.append(prompt) or ("" if "'garden'" in prompt else "Page."))
    entries("garden", 6)
    entries("water", 5)
    assert engine.compile_due() == ("failed", None)
    os.utime(store / ".compile-due", (0, 0))
    assert engine.compile_due()[0] == "compiled"
    assert "'water'" in calls[-1]


def test_a_compile_folds_at_most_the_cap_and_its_own_page_first(store, monkeypatch):
    calls = llm(monkeypatch)
    monkeypatch.setattr(engine, "COMPILE_MAX_ENTRIES", 4)
    entries("garden", 3)
    page = engine.compile_tag("garden")
    later = entries("garden", 5, start=3)
    engine.compile_tag("garden")
    assert page in calls[-1]
    assert sum(status(sid) == "active" for sid in later) == 2


def test_a_source_retired_during_the_model_call_keeps_its_successor(store, monkeypatch):
    sources = entries("garden", 3)
    correction = engine.store_entry("Synthetic correction", "Synthetic garden correction.")

    def call(prompt, **kwargs):
        engine.supersede(sources[0], correction)
        return "Synthetic page."

    monkeypatch.setattr(engine, "_call_llm", call)
    page = engine.compile_tag("garden")
    assert engine._parse_frontmatter(engine.get(sources[0]))["superseded_by"] == correction
    assert engine._parse_frontmatter(engine.get(sources[1]))["superseded_by"] == page
