import json
import os
import subprocess
import sys
from pathlib import Path

import cli
import engine
import hygiene
import pytest


def test_store_sections_preserve_content_and_fenced_headings(store):
    content = "## Verified Protocol\nFirst.\n## Edge Cases\nSecond.\n## Edge Cases\nThird."
    node = engine.store_entry("Sections", content, tags=["test"])
    text = engine.get(node)
    assert text.count("## Verified Protocol") == 1
    assert text.count("## Edge Cases") == 1
    assert all(word in text for word in ("First.", "Second.", "Third."))
    node = engine.store_entry("Code", "```md\n## History\n```", tags=["test"])
    text = engine.get(node)
    assert "```md\n## History\n```" in text
    assert text.count("## History") == 2
    assert "Initial entry." in text


def test_supersede_twice_is_precise_and_survives_reindex(store):
    old = engine.store_entry("Old", "Example status: active\ntags: [example]", tags=["test"])
    a = engine.store_entry("A", "First replacement", tags=["test"])
    b = engine.store_entry("B", "Final replacement", tags=["test"])
    engine.supersede(old, a)
    engine.supersede(old, b)
    text = engine.get(old)
    assert text.count("superseded_by:") == 1
    assert f"superseded_by: {b}" in text
    assert "Example status: active\ntags: [example]" in text
    engine.supersede(old, b)
    assert engine.get(old) == text
    engine.reindex()
    assert hygiene.index_disagreement() is None
    conn = engine._connect()
    assert conn.execute("SELECT superseded_by FROM nodes WHERE id=?", (old,)).fetchone()[0] == b
    conn.close()


def test_check_repairs_duplicate_sections_and_pointers(store):
    old = engine.store_entry("Old", "Original", tags=["test"])
    a = engine.store_entry("A", "One replacement", tags=["test"])
    b = engine.store_entry("B", "Two replacement", tags=["test"])
    engine.supersede(old, b)
    path = engine._node_path(old)
    path.write_text(engine.get(old).replace(f"superseded_by: {b}", f"superseded_by: {b}\nsuperseded_by: {a}") + "\n## Edge Cases\nKeep this detail.\n")
    problems = hygiene.run_audit()
    assert any("Duplicate sections" in p for p in problems)
    assert any("Duplicate superseded_by" in p for p in problems)
    hygiene.run_audit(fix=True)
    assert hygiene.run_audit() == []
    assert hygiene.index_disagreement() is None
    assert "Keep this detail." in engine.get(old)
    assert engine._parse_frontmatter(engine.get(old))["superseded_by"] == b


def test_check_repairs_index_from_valid_markdown(store):
    old = engine.store_entry("Old", "Original", tags=["test"])
    new = engine.store_entry("New", "Replacement", tags=["test"])
    engine.supersede(old, new)
    conn = engine._connect()
    conn.execute("UPDATE nodes SET superseded_by='' WHERE id=?", (old,))
    conn.commit()
    conn.close()
    assert "superseded_by" in hygiene.index_disagreement()
    hygiene.run_audit(fix=True)
    assert hygiene.index_disagreement() is None
    assert new in engine.get(old)


def test_list_limit_newest_first_with_same_day_tie(store, capsys):
    ids = [engine.store_entry(str(i), f"Unique {i}") for i in range(4)]
    assert cli.main(["list", "--limit", "2", "--json"]) == 0
    assert [n["id"] for n in json.loads(capsys.readouterr().out)] == ids[::-1][:2]
    assert engine.list_nodes(limit=0) == []


def test_concurrent_writers_do_not_reuse_ids(store, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(Path(engine.__file__).parents[2] / "lib"))
    program = Path(engine.__file__).with_name("k7e.py")
    processes = [subprocess.Popen([sys.executable, str(program), "store", str(i), "--content", f"Concurrent entry {i}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(12)]
    for process in processes:
        out, err = process.communicate(timeout=30)
        assert process.returncode == 0, (out, err)
    assert len(engine.list_nodes()) == 12
    assert len(list(engine._all_node_files())) == 12


def test_crashed_append_recovers_once_without_undoing_later_changes(store):
    node = engine.store_entry("Deployment", "The release target is cobalt.")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([
        str(Path(engine.__file__).parent), str(Path(engine.__file__).parents[2] / "lib")])}
    script = (
        "import os, engine; engine.init(); "
        "engine._index_node=lambda *a, **k: os._exit(99); "
        f"engine.run_operation('capture/append', engine.append_entry, {node!r}, 'Edge Cases', 'Only on Tuesdays.')"
    )
    result = subprocess.run([sys.executable, "-c", script], env=env, timeout=20)
    assert result.returncode == 99
    assert list((store / ".operations").glob("*.pending.json"))
    engine.append_entry(node, "Edge Cases", "Use the signed build.")
    engine.run_operation("capture/append", engine.append_entry, node, "Edge Cases", "Only on Tuesdays.")
    text = engine.get(node)
    assert text.count("Only on Tuesdays.") == 1
    assert "Use the signed build." in text
    assert not list((store / ".operations").glob("*.pending.json"))
    assert hygiene.index_disagreement() is None


def test_engine_capture_extracts_short_facts_without_injected_memory(store, monkeypatch):
    import distill
    capture = store / "capture.json"
    capture.write_text(json.dumps({
        "format": "r4t-memory-turn-v1", "stamp": "one", "root": str(store),
        "input": "The launch date is October 3.", "output": "Understood.",
        "knowledge": [], "human_messages": [], "exit": 0,
        "injected": "A recalled fact must not be extracted again.",
    }))
    prompts = []
    monkeypatch.setattr(distill, "_run_llm_prompt", lambda text: prompts.append(text) or [])
    monkeypatch.setenv("K7E_DISTILL_COMMAND", "unused")
    distill.extract_from_file(capture)
    assert len(prompts) == 1
    assert "October 3" in prompts[0]
    assert "A recalled fact" not in prompts[0]


def test_job_replay_is_portable_and_rejects_changed_input(store, monkeypatch):
    import distill
    capture = store / "turn.txt"
    capture.write_text("The deployment cluster is cobalt for staging releases.")
    monkeypatch.setattr(distill, "extract_from_file", lambda _: [{
        "title": "Deployment cluster", "content": "The deployment cluster is cobalt for staging releases.", "tags": ["deploy"],
    }])
    engine.reset_llm_failures()
    result = distill.distill([capture], job_id="turn-one")
    assert result[0]["action"] == "stored"
    moved = capture.rename(store / "moved.txt")
    monkeypatch.setattr(distill, "extract_from_file", lambda _: pytest.fail("completed plan extracted again"))
    assert distill.distill([moved], job_id="turn-one") == [{**result[0], "source": str(moved)}]
    assert len(engine.list_nodes()) == 1
    moved.write_text("Different input under a reused job identity.")
    rejected = distill.distill([moved], job_id="turn-one")
    assert rejected[0]["action"] == "skipped"
    assert "different input" in rejected[0]["reason"]
    with pytest.raises(ValueError, match="one immutable file"):
        distill.distill([moved, moved], job_id="turn-one")
