"""A compile replaces the entries it folds, and a scheduled compile runs when due."""
import json
import time
from pathlib import Path

import cli
import distill
import engine


PAGE = ("## Overview\n\nSynthetic compiled reference about irrigation.\n\n## Procedures\n\n"
        "* Water the synthetic beds in order.\n* Close the synthetic valve after each run.\n\n"
        "## Gotchas\n\nA synthetic valve left open drains the tank.\n")


def llm(monkeypatch, calls=None, reply=PAGE):
    calls = [] if calls is None else calls
    monkeypatch.setattr(engine, "_call_llm", lambda prompt, **kwargs: calls.append(prompt) or reply)
    return calls


def age_mark(store, hours):
    """Move the last due pass `hours` into the past: the pass time lives in the mark's JSON."""
    mark = store / ".compile-due"
    record = json.loads(mark.read_text(encoding="utf-8"))
    record["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
    mark.write_text(json.dumps(record) + "\n", encoding="utf-8")


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
    age_mark(store, engine.COMPILE_DUE_HOURS + 1)
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
    age_mark(store, engine.COMPILE_DUE_HOURS + 1)
    assert engine.compile_due()[0] == "compiled"
    assert task not in calls[-1] and status(task) == "active"


def test_after_a_failed_tag_the_next_pass_tries_another(store, monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "_call_llm",
                        lambda prompt, **kwargs: calls.append(prompt) or ("" if "'garden'" in prompt else PAGE))
    entries("garden", 6)
    entries("water", 5)
    assert engine.compile_due() == ("failed", None)
    age_mark(store, engine.COMPILE_DUE_HOURS + 1)
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
        return PAGE

    monkeypatch.setattr(engine, "_call_llm", call)
    page = engine.compile_tag("garden")
    assert engine._parse_frontmatter(engine.get(sources[0]))["superseded_by"] == correction
    assert engine._parse_frontmatter(engine.get(sources[1]))["superseded_by"] == page


def test_the_due_interval_runs_on_the_pinned_clock(store, monkeypatch):
    llm(monkeypatch)
    entries("garden", 4)
    monkeypatch.setenv("K7E_NOW", "2026-01-01T00:00:00Z")
    engine.compile_due()
    entries("garden", 1, start=4)
    monkeypatch.setenv("K7E_NOW", "2026-01-01T19:00:00Z")
    assert engine.compile_due() == ("not-due", None)
    monkeypatch.setenv("K7E_NOW", "2026-01-01T21:00:00Z")
    assert engine.compile_due()[0] == "compiled"


def test_the_compile_prompt_carries_each_sources_date(store, monkeypatch):
    calls = llm(monkeypatch)
    for day in ("2026-03-04", "2026-05-06", "2026-07-08"):
        engine.store_entry(f"Synthetic garden rule {day}", f"Synthetic garden irrigation rule {day}.",
                           tags=["garden"], valid_from=f"{day}T09:00:00Z")
    engine.compile_tag("garden")
    for day in ("2026-03-04", "2026-05-06", "2026-07-08"):
        assert f", {day}] Synthetic garden rule {day}" in calls[0]
    assert "superseded, with their dates" in calls[0]


def test_a_correction_naming_a_compiled_page_does_not_retire_it(store, monkeypatch, tmp_path):
    llm(monkeypatch)
    entries("garden", 3)
    page = engine.compile_tag("garden")
    asked = []
    correction = [{"id": page, "title": "Synthetic garden valve change",
                   "content": "Synthetic garden valve moved to the north bed zebra.", "quote": "north bed"}]
    monkeypatch.setenv("K7E_DISTILL_COMMAND", "synthetic-distill")
    monkeypatch.setattr(engine, "_call_llm", lambda prompt, **kwargs: asked.append(prompt) or (
        json.dumps(correction) if "## The recalled notes" in prompt else "[]"))
    capture = tmp_path / "turn.md"
    capture.write_text(
        "# turn 20261014T163000000000Z (Synthetic)\n\n- stamp: 20261014T163000000000Z\n- exit: 0\n"
        f"- knowledge: {page}\n\n## Human messages\n\n### From: owner (thread 01X)\n\n"
        "The valve moved to the north bed.\n\n## Prompt\n\nPrompt text.\n\n## Output\n\nOk.\n",
        encoding="utf-8")
    distill.distill([str(capture)])
    assert any("## The recalled notes" in p for p in asked)
    assert status(page) == "active"
    assert "north bed zebra" in engine.get(page)
    assert page in [h["id"] for h in engine.search("garden valve zebra", limit=5)]


NARRATION = ("I'll check the workspace and write the reference.\n\nI wrote the synthesis to "
             "writer/garden.md and summarized it here.\n")


def test_a_reply_that_narrates_supersedes_nothing(store, monkeypatch, capsys):
    monkeypatch.setattr(engine, "_llm_failures", [])
    llm(monkeypatch, reply=NARRATION)
    sources = entries("garden", 3)
    assert engine.compile_tag("garden") is None
    assert all(status(sid) == "active" for sid in sources)
    assert "rejected the model's reply for 'garden': it narrates" in capsys.readouterr().err
    assert ("compile", "reply rejected: it narrates in the first person") in engine._llm_failures
    assert not [n for n in engine.list_nodes(status="active") if "compiled" in (n.get("tags") or [])]


def test_a_reply_without_sections_or_shorter_than_a_source_is_rejected(store, monkeypatch, capsys):
    sources = entries("garden", 3)
    llm(monkeypatch, reply="Synthetic reference about irrigation that states a few procedures plainly.")
    assert engine.compile_tag("garden") is None
    assert "it has no sections" in capsys.readouterr().err
    llm(monkeypatch, reply="## Overview\n\nShort.\n")
    assert engine.compile_tag("garden") is None
    assert "shorter than its smallest source" in capsys.readouterr().err
    assert all(status(sid) == "active" for sid in sources)


def test_a_quoted_person_deep_in_a_page_is_not_narration(store, monkeypatch):
    body = PAGE + "\n## Gotchas\n\n" + "Detail. " * 60 + "The owner said: I'll water on Mondays.\n"
    llm(monkeypatch, reply=body)
    entries("garden", 3)
    assert engine.compile_tag("garden")


def test_a_rejected_due_pass_leaves_sources_active_and_waits(store, monkeypatch):
    llm(monkeypatch, reply=NARRATION)
    sources = entries("garden", 5)
    assert engine.compile_due() == ("failed", None)
    assert all(status(sid) == "active" for sid in sources)
    assert engine.compile_due() == ("not-due", None)


def test_the_prompt_asks_for_the_page_and_nothing_else(store, monkeypatch):
    calls = llm(monkeypatch)
    entries("garden", 3)
    engine.compile_tag("garden")
    assert "no preamble" in calls[0] and "no file written anywhere" in calls[0]


def timeouts_above(monkeypatch, limit, calls):
    import subprocess

    def call(prompt, **kwargs):
        calls.append(prompt)
        if prompt.count("[Entry ") > limit:
            raise subprocess.TimeoutExpired("llm", kwargs.get("timeout"))
        return PAGE

    monkeypatch.setattr(engine, "_call_llm", call)


def test_a_timed_out_compile_retries_on_half_the_entries(store, monkeypatch, capsys):
    calls = []
    timeouts_above(monkeypatch, 4, calls)
    sources = entries("garden", 12)
    page = engine.compile_tag("garden")
    assert page
    assert [c.count("[Entry ") for c in calls] == [12, 6, 3]
    folded = [sid for sid in sources if status(sid) == "superseded"]
    assert len(folded) == 3 and all(status(sid) == "active" for sid in sources if sid not in folded)
    assert sorted(engine._parse_frontmatter(engine.get(page))["compiled_from"]) == sorted(folded)
    err = capsys.readouterr().err
    assert "timed out at 12 entries; retrying with 6" in err and "timed out at 6 entries; retrying with 3" in err


def test_a_compile_that_times_out_at_the_floor_supersedes_nothing(store, monkeypatch, capsys):
    calls = []
    timeouts_above(monkeypatch, 0, calls)
    sources = entries("garden", 5)
    assert engine.compile_tag("garden") is None
    assert [c.count("[Entry ") for c in calls] == [5, 3]
    assert all(status(sid) == "active" for sid in sources)
    assert "timed out at 3 entries; nothing superseded" in capsys.readouterr().err


def test_a_small_tag_that_times_out_is_still_tried_at_the_floor(store, monkeypatch):
    calls = []
    timeouts_above(monkeypatch, 3, calls)
    sources = entries("garden", 5)
    assert engine.compile_tag("garden")
    assert [c.count("[Entry ") for c in calls] == [5, 3]
    assert sum(status(sid) == "superseded" for sid in sources) == 3


def test_a_real_timeout_reaches_the_compile_as_a_timeout(store, tmp_path, monkeypatch):
    import subprocess
    from conftest import write_path_executable
    stub = write_path_executable(tmp_path / "bin", "slow-llm", "import time; time.sleep(30)")
    monkeypatch.setenv("K7E_LLM_COMMAND", str(stub))
    monkeypatch.setattr(engine, "_llm_failures", [])
    assert engine._call_llm("prompt", purpose="compile", timeout=1) is None
    try:
        engine._call_llm("prompt", purpose="compile", timeout=1, timeout_raises=True)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("a timeout the caller asked for did not raise")
    assert engine._llm_failures.count(("compile", "timed out (1s)")) == 2


def test_the_model_runs_in_a_scratch_directory_that_is_gone_afterwards(store, tmp_path, monkeypatch):
    from conftest import write_path_executable
    stub = write_path_executable(tmp_path / "bin", "writer-llm",
                                 "import os, pathlib\n"
                                 "pathlib.Path('writer').mkdir()\n"
                                 "pathlib.Path('writer/garden.md').write_text('synthesis')\n"
                                 "print('## Overview\\n\\n' + os.getcwd())")
    monkeypatch.setenv("K7E_LLM_COMMAND", str(stub))
    reply = engine._call_llm("prompt", purpose="compile")
    cwd = reply.split("\n")[-1]
    assert cwd != str(store) and not Path(cwd).exists()
    assert not (store / "writer").exists()


def test_revert_restores_the_sources_and_retires_the_page(store, monkeypatch):
    llm(monkeypatch)
    sources = entries("garden", 3)
    page = engine.compile_tag("garden")
    assert sorted(engine.revert_compile(page)) == sorted(sources)
    for sid in sources:
        meta = engine._parse_frontmatter(engine.get(sid))
        assert meta["status"] == "active" and "superseded_by" not in meta and "superseded_at" not in meta
    meta = engine._parse_frontmatter(engine.get(page))
    assert meta["status"] == "superseded" and "superseded_by" not in meta and meta["superseded_at"]
    assert "Reverted; 3 sources restored to active." in engine.get(page)
    hits = [h["id"] for h in engine.search("irrigation", limit=10)]
    assert set(sources) <= set(hits) and page not in hits
    conn = engine._connect()
    rows = dict(conn.execute("SELECT id, status FROM nodes").fetchall())
    edges = conn.execute("SELECT src, dst FROM edges WHERE type = 'superseded_by'").fetchall()
    conn.close()
    assert all(rows[sid] == "active" for sid in sources) and rows[page] == "superseded"
    assert not [e for e in edges if e[1] == page]
    assert engine.revert_compile(page) == []


def test_revert_keeps_a_source_a_correction_retired(store, monkeypatch):
    llm(monkeypatch)
    sources = entries("garden", 3)
    page = engine.compile_tag("garden")
    correction = engine.store_entry("Synthetic correction", "Synthetic garden correction.")
    engine.supersede(sources[0], correction)
    assert sorted(engine.revert_compile(page)) == sorted(sources[1:])
    assert engine._parse_frontmatter(engine.get(sources[0]))["superseded_by"] == correction


def test_revert_refuses_what_is_not_a_compiled_page(store, monkeypatch, capsys):
    plain = entries("garden", 1)[0]
    assert engine.revert_compile(plain) is None
    assert engine.revert_compile("K7E-000-99999") is None
    assert cli.main(["compile", "--revert", plain]) == 1
    assert "not a compiled page" in capsys.readouterr().err
    assert cli.main(["compile", "garden", "--revert", plain]) == 2
    assert cli.main(["compile", "--revert", plain, "--dry-run"]) == 2


def test_cli_revert_reports_the_count(store, monkeypatch, capsys):
    llm(monkeypatch)
    entries("garden", 3)
    page = engine.compile_tag("garden")
    assert cli.main(["compile", "--revert", page]) == 0
    assert f"Reverted {page}: 3 sources restored to active." in capsys.readouterr().out


def test_revert_writes_its_history_line_under_the_history_section(store, monkeypatch):
    llm(monkeypatch)
    entries("garden", 3)
    page = engine.compile_tag("garden")
    engine.append_entry(page, "Corrections", "Synthetic correction appended after the compile.")
    engine.revert_compile(page)
    text = engine.get(page)
    history = text.split("## History", 1)[1].split("\n## ", 1)[0]
    assert "Reverted; 3 sources restored to active." in history
    assert "Synthetic correction appended after the compile." in text.split("## Corrections", 1)[1]


def test_revert_refuses_a_page_a_later_compile_folded(store, monkeypatch, capsys):
    import pytest
    llm(monkeypatch)
    first = entries("garden", 3)
    page = engine.compile_tag("garden")
    later = entries("garden", 2, start=3)
    again = engine.compile_tag("garden")
    assert engine._parse_frontmatter(engine.get(page))["superseded_by"] == again
    with pytest.raises(engine.RevertRefused, match=f"{page} is superseded by {again}; revert {again} first"):
        engine.revert_compile(page)
    assert all(status(sid) == "superseded" for sid in first) and status(again) == "active"
    assert cli.main(["compile", "--revert", page]) == 1
    assert f"revert {again} first" in capsys.readouterr().err
    # The chain unwinds from the current page: reverting it makes the first
    # page active again, and only then do its own sources come back.
    assert sorted(engine.revert_compile(again)) == sorted([page, *later])
    assert status(page) == "active" and all(status(sid) == "active" for sid in later)
    assert all(status(sid) == "superseded" for sid in first)
    assert sorted(engine.revert_compile(page)) == sorted(first)
    assert all(status(sid) == "active" for sid in first)
    hits = [h["id"] for h in engine.search("irrigation", limit=10)]
    assert again not in hits and page not in hits
