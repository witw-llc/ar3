"""Two clocks and a graph: every entry records when the store learned it and
when it became true, retirements are edges, and a search can slice the store
by either clock."""
import json
import re

import cli
import distill
import engine
from test_pebbles import STAMP, model, roster_capture


def clock(monkeypatch, *stamps):
    """Make the record clock read `stamps` in order, one per write."""
    queue = list(stamps)
    monkeypatch.setattr(engine, "utc_now", lambda: queue.pop(0))


def meta(node_id):
    return engine._parse_frontmatter(engine.get(node_id))


def ids(**kwargs):
    return {h["id"] for h in engine.search("widget service deploy", limit=10, **kwargs)}


def replaced(monkeypatch):
    """A rule learned in January, replaced by one learned in July that has
    been true since June."""
    clock(monkeypatch, "2026-01-10T12:00:00Z", "2026-07-10T12:00:00Z", "2026-07-10T12:00:00Z")
    old = engine.store_entry("Widget service deploy", "The widget service deploy runs from main.",
                             valid_from="2026-01-01T00:00:00Z")
    new = engine.store_entry("Widget service deploy rule", "The widget service deploy runs from the release branch.",
                             valid_from="2026-06-01T00:00:00Z")
    engine.supersede(old, new)
    return old, new


def test_an_entry_records_when_the_store_learned_it(store):
    node = engine.store_entry("Widget build tool", "The widget service builds with SDK 10.")
    stamps = meta(node)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamps["recorded_at"])
    assert stamps["valid_from"] == stamps["recorded_at"]


def test_a_retirement_is_dated_and_is_an_edge(store, monkeypatch):
    old, new = replaced(monkeypatch)
    assert meta(old)["superseded_at"] == "2026-07-10T12:00:00Z"
    node = engine.graph(old)
    assert node["out"] == [{"type": "superseded_by", "id": new, "title": "Widget service deploy rule",
                            "status": "active"}]
    assert [e["id"] for e in engine.graph(new)["in"]] == [old]
    [hit] = [h for h in engine.search("widget service deploy", limit=5) if h["id"] == new]
    assert hit["replaces"] == [old] and hit["valid_from"] == "2026-06-01T00:00:00Z"


def test_as_of_reads_what_the_store_held_then(store, monkeypatch):
    old, new = replaced(monkeypatch)
    assert ids(as_of="2026-01-01T00:00:00Z") == set()
    assert ids(as_of="2026-03-01T00:00:00Z") == {old}
    assert ids(as_of="2026-08-01T00:00:00Z") == {new}
    assert ids() == {new}


def test_valid_at_reads_what_was_true_then(store, monkeypatch):
    old, new = replaced(monkeypatch)
    assert ids(valid_at="2026-03-01T00:00:00Z") == {old}
    assert ids(valid_at="2026-06-15T00:00:00Z") == {new}
    assert ids(valid_at="2025-12-01T00:00:00Z") == set()


def test_both_clocks_together_read_what_the_store_believed_then(store, monkeypatch):
    """In March the store did not yet know of the June replacement, so it
    believed the January rule held in June."""
    old, new = replaced(monkeypatch)
    assert ids(as_of="2026-03-01T00:00:00Z", valid_at="2026-06-15T00:00:00Z") == {old}
    assert ids(as_of="2026-08-01T00:00:00Z", valid_at="2026-06-15T00:00:00Z") == {new}


def test_both_clocks_keep_a_retired_rule_for_the_time_before_its_replacement(store, monkeypatch):
    """By August the store knew the January rule was replaced from June, so it
    still held that the January rule governed March."""
    old, new = replaced(monkeypatch)
    assert ids(as_of="2026-08-01T00:00:00Z", valid_at="2026-03-01T00:00:00Z") == {old}
    assert ids(as_of="2026-03-01T00:00:00Z", valid_at="2026-03-01T00:00:00Z") == {old}
    assert ids(as_of="2026-08-01T00:00:00Z", valid_at="2025-12-01T00:00:00Z") == set()


def test_an_expired_task_fact_is_still_true_inside_its_window(store, monkeypatch):
    clock(monkeypatch, "2026-09-10T12:00:00Z")
    task = engine.store_entry("Widget service deploy freeze", "The widget service deploy is frozen this week.",
                              valid_from="2026-09-10T12:00:00Z", valid_to="2026-09-17")
    assert task not in ids()
    assert task in ids(valid_at="2026-09-15T12:00:00Z")
    assert task not in ids(valid_at="2026-09-20T12:00:00Z")
    assert task in ids(as_of="2026-09-15T12:00:00Z")
    assert task not in ids(as_of="2026-09-20T12:00:00Z")


def test_a_compiled_page_records_its_sources_and_stands_for_them(store, monkeypatch):
    monkeypatch.setattr(engine, "_call_llm", lambda prompt, **kwargs: "Widget service deploy summary.")
    sources = [engine.store_entry(f"Widget service deploy {n}", f"Widget service deploy fact {n}.",
                                  tags=["widget"], valid_from=f"2026-0{n}-01T00:00:00Z") for n in (3, 1, 2)]
    page = engine.compile_tag("widget")
    stamps = meta(page)
    assert stamps["valid_from"] == "2026-03-01T00:00:00Z"
    out = engine.graph(page)["out"]
    assert {(e["type"], e["id"]) for e in out} == {("compiled_from", s) for s in sources}
    for source in sources:
        assert {(e["type"], e["id"]) for e in engine.graph(source)["in"]} == {("compiled_from", page)}
    assert ids(valid_at="2026-02-15T00:00:00Z") == set(sources[1:])
    assert ids(valid_at="2026-03-15T00:00:00Z") == {page}


def test_a_wikilink_is_a_mention(store):
    target = engine.store_entry("Widget build tool", "The widget service builds with SDK 10.")
    node = engine.store_entry("Widget release notes", f"The release follows [[{target}]] and [[{target}]].")
    assert engine.graph(node)["out"] == [{"type": "mentions", "id": target, "title": "Widget build tool",
                                          "status": "active"}]


def test_an_entry_written_before_the_clocks_is_dated_by_the_rebuild(store, monkeypatch):
    old, new = replaced(monkeypatch)
    for node in (old, new):
        path = engine._node_path(node)
        text = path.read_text(encoding="utf-8")
        path.write_text(re.sub(r"(?m)^(recorded_at|valid_from|superseded_at): .*\n", "", text), encoding="utf-8")
    engine.reindex()
    day = meta(new)["last_updated"]
    node = engine.graph(old)
    assert node["recorded_at"] == f"{day}T00:00:00Z" and node["superseded_at"] == f"{day}T00:00:00Z"
    assert node["out"][0]["id"] == new


def test_reindex_rebuilds_the_edges(store, monkeypatch):
    old, new = replaced(monkeypatch)
    engine.reindex()
    assert [e["id"] for e in engine.graph(new)["in"]] == [old]


def test_distill_dates_a_fact_by_its_turn(store, tmp_path, monkeypatch):
    model(monkeypatch, [{"title": "Widget build tool", "content": "The widget service builds with SDK 10.",
                         "tags": ["widget"], "lifespan": "durable"}])
    [result] = distill.distill([str(roster_capture(tmp_path))])
    assert meta(result["id"])["valid_from"] == "2026-10-14T16:30:00Z"
    assert meta(result["id"])["recorded_at"] != "2026-10-14T16:30:00Z"


def test_a_bare_date_means_the_end_of_that_local_day():
    stamp = engine.utc_stamp("2026-03-01")
    assert engine._local_day(stamp) == "2026-03-01"
    assert engine.utc_stamp("2026-03-01T10:00:00Z") == "2026-03-01T10:00:00Z"


def test_the_cli_slices_and_shows_the_graph(store, monkeypatch, capsys):
    old, new = replaced(monkeypatch)
    assert cli.main(["search", "widget service deploy", "--as-of", "2026-03-01", "--ids"]) == 0
    assert capsys.readouterr().out.split() == [old]
    assert cli.main(["search", "widget service deploy", "--valid-at", "2026-03-01", "--json"]) == 0
    [hit] = json.loads(capsys.readouterr().out)
    assert hit["id"] == old and hit["status"] == "superseded"
    assert cli.main(["search", "widget", "--as-of", "last tuesday"]) == 2
    assert "not a date or time" in capsys.readouterr().err
    assert cli.main(["graph", old]) == 0
    out = capsys.readouterr().out
    assert f"superseded_by -> {new}" in out and "superseded_at: 2026-07-10T12:00:00Z" in out
    assert cli.main(["graph", "K7E-000-99999"]) == 1


def test_a_slice_value_that_is_not_a_stamp_never_reaches_the_sql(store):
    import pytest
    with pytest.raises(ValueError):
        engine.search("widget", as_of="2026-01-01T00:00:00Z' OR '1'='1", valid_at="2026-01-01T00:00:00Z")


def test_a_pinned_clock_moves_expiry_and_the_stamp(store, monkeypatch):
    monkeypatch.setenv("K7E_NOW", "2031-03-04T20:06:07Z")
    assert engine.utc_now() == "2031-03-04T20:06:07Z"
    expired = engine.store_entry("Widget run", "The widget service run passed.", valid_to="2031-03-03")
    live = engine.store_entry("Widget gate", "The widget gate rejects stale changelogs.", valid_to="2031-03-05")
    ids = {h["id"] for h in engine.search("widget", limit=10)}
    assert live in ids and expired not in ids
    monkeypatch.setenv("K7E_NOW", "2031-03-07T12:00:00Z")
    assert live not in {h["id"] for h in engine.search("widget", limit=10)}


def test_a_pinned_clock_must_be_a_utc_stamp(monkeypatch):
    monkeypatch.setenv("K7E_NOW", "2031-03-04")
    import pytest
    with pytest.raises(ValueError):
        engine.now()
