"""Triage: a candidate close to an active entry is sorted by the model as a
duplicate, a detail, a replacement or a separate fact, and a replacement
retires what it replaces — an open idea included."""
import json

import distill
import engine
from test_pebbles import model as extraction, roster_capture

RULE = "The widget service deploys from the release branch with make deploy."
EARLIER = "2026-10-01T00:00:00Z"  # before the shared capture's turn, 2026-10-14T16:30Z


def triage(monkeypatch, verdicts, items=()):
    """Answer a triage prompt with `verdicts` (one per pair, in order) and an
    extraction prompt with `items`. Returns the triage prompts seen."""
    monkeypatch.setenv("K7E_DISTILL_COMMAND", "synthetic-distill")
    asked = []

    def answer(prompt, **kwargs):
        if "Classify each pair" in prompt:
            asked.append(prompt)
            return json.dumps([{"pair": n, "verdict": v} for n, v in enumerate(verdicts, 1)])
        return json.dumps(list(items))
    monkeypatch.setattr(engine, "_call_llm", answer)
    return asked


def candidate(content, **extra):
    return {"title": "Widget service deploy", "content": content, **extra}


def test_a_replacement_retires_the_entry_it_replaces(store, tmp_path, monkeypatch):
    old = engine.store_entry("Widget service deploy", RULE, valid_from=EARLIER)
    triage(monkeypatch, ["replacement"], [candidate(
        "The widget service deploys from the main branch with make deploy.", tags=["widget"])])
    [result] = distill.distill([str(roster_capture(tmp_path))])
    assert result["action"] == "superseded" and result["old_id"] == old
    new = result["id"]
    assert engine._parse_frontmatter(engine.get(old))["superseded_by"] == new
    assert engine._parse_frontmatter(engine.get(new))["confidence"] == 0.5
    assert [e["id"] for e in engine.graph(new)["in"]] == [old]


def test_a_lone_verdict_object_is_read(store, monkeypatch):
    old = engine.store_entry("Widget service deploy", RULE)
    monkeypatch.setenv("K7E_DISTILL_COMMAND", "synthetic-distill")
    monkeypatch.setattr(engine, "_call_llm", lambda prompt, **kwargs: '{"pair": 1, "verdict": "detail"}')
    [kept] = distill.diff_against_store([candidate(RULE)])
    assert kept["_append_to"] == old
    monkeypatch.setattr(engine, "_call_llm", lambda prompt, **kwargs: '{"verdicts": [{"pair": 1, "verdict": "detail"}]}')
    [kept] = distill.diff_against_store([candidate(RULE)])
    assert kept["_append_to"] == old


def test_a_detail_is_appended_and_a_duplicate_is_dropped(store, monkeypatch):
    old = engine.store_entry("Widget service deploy", RULE)
    triage(monkeypatch, ["detail"])
    [kept] = distill.diff_against_store([candidate(RULE + " It takes four minutes.")])
    assert kept["_append_to"] == old
    triage(monkeypatch, ["duplicate"])
    assert distill.diff_against_store([candidate(RULE + " It takes four minutes.")]) == []


def test_a_separate_fact_is_stored_beside_the_entry(store, monkeypatch):
    engine.store_entry("Widget service deploy", RULE)
    triage(monkeypatch, ["separate"])
    [kept] = distill.diff_against_store([candidate(RULE)])
    assert "_append_to" not in kept and "_supersedes" not in kept


def test_without_a_verdict_the_overlap_rule_decides(store, monkeypatch):
    old = engine.store_entry("Widget service deploy", RULE)
    asked = triage(monkeypatch, [])
    assert distill.diff_against_store([candidate(RULE)]) == []
    assert len(asked) == 1
    monkeypatch.delenv("K7E_DISTILL_COMMAND")
    monkeypatch.setattr(engine, "_call_llm", lambda *a, **k: None)
    detail = ("The widget service deploys from the release branch with make deploy, "
              "and staging quota blocks rollout during business hours twice weekly.")
    [kept] = distill.diff_against_store([{"title": "Rollout quota windows", "content": detail}])
    assert kept["_append_to"] == old


def test_one_call_triages_every_pair_and_none_without_pairs(store, monkeypatch):
    engine.store_entry("Widget service deploy", RULE)
    engine.store_entry("Gadget build tool", "The gadget service builds with toolkit nine on release tags.")
    asked = triage(monkeypatch, ["duplicate", "duplicate"])
    kept = distill.diff_against_store([
        candidate(RULE),
        {"title": "Gadget build tool", "content": "The gadget service builds with toolkit nine on release tags."},
        {"title": "Orchard watering", "content": "The orchard sprinklers run at dawn on Tuesdays."},
    ])
    assert len(asked) == 1 and "Pair 2" in asked[0]
    assert [k["title"] for k in kept] == ["Orchard watering"]
    asked = triage(monkeypatch, [])
    distill.diff_against_store([{"title": "Harbor tides", "content": "Harbor tides peak near noon in spring."}])
    assert asked == []


def test_a_task_fact_never_replaces_a_durable_entry(store, monkeypatch):
    engine.store_entry("Widget service deploy", RULE)
    triage(monkeypatch, ["replacement"])
    [kept] = distill.diff_against_store([candidate(
        "The widget service deploys from the main branch with make deploy today.", lifespan="task")])
    assert "_supersedes" not in kept and "_append_to" not in kept


def test_one_fact_never_retires_a_compiled_page(store, monkeypatch):
    page = engine.store_entry("Widget service deploy", RULE, tags=["widget", "compiled"])
    triage(monkeypatch, ["replacement"])
    [kept] = distill.diff_against_store([candidate(
        "The widget service deploys from the main branch with make deploy.")])
    assert kept["_append_to"] == page and "_supersedes" not in kept


def test_a_fact_that_settles_an_idea_closes_it(store, tmp_path, monkeypatch):
    idea = engine.store_entry("Orchard sprinkler timer idea",
                              "Maybe the orchard sprinklers should run on a dawn timer.", kind="idea",
                              valid_from=EARLIER)
    triage(monkeypatch, ["replacement"], [{
        "title": "Orchard sprinkler timer", "tags": ["orchard"],
        "content": "The orchard sprinklers now run on a dawn timer, set at six."}])
    [result] = distill.distill([str(roster_capture(tmp_path))])
    assert result["action"] == "superseded" and result["old_id"] == idea
    open_ideas = [h for h in engine.search("orchard sprinklers dawn timer", limit=10, include_archive=True)
                  if h["kind"] == "idea"]
    assert open_ideas == []


def test_a_decision_record_can_close_an_idea(store, monkeypatch):
    idea = engine.store_entry("Orchard sprinkler timer idea",
                              "Maybe the orchard sprinklers should run on a dawn timer.", kind="idea")
    triage(monkeypatch, ["replacement"])
    [kept] = distill.diff_against_store([{
        "title": "Orchard sprinkler timer decision", "kind": "decision",
        "content": "Decided: the orchard sprinklers run on a dawn timer."}])
    assert kept["_supersedes"] == idea


def test_an_idea_stays_open_when_the_fact_is_separate(store, monkeypatch):
    engine.store_entry("Orchard sprinkler timer idea",
                       "Maybe the orchard sprinklers should run on a dawn timer.", kind="idea")
    triage(monkeypatch, ["separate"])
    [kept] = distill.diff_against_store([{
        "title": "Orchard sprinkler pressure", "content": "The orchard sprinklers run at forty psi on a dawn timer."}])
    assert "_supersedes" not in kept


def test_the_extractor_mock_still_reads_the_store(store, tmp_path, monkeypatch):
    """A model that answers every prompt with extraction items gives triage no
    verdict, so the overlap rule still decides."""
    engine.store_entry("Widget service deploy", RULE)
    extraction(monkeypatch, [candidate(RULE, tags=["widget"])])
    assert distill.distill([str(roster_capture(tmp_path))]) == []


def test_a_value_that_flips_back_leaves_the_subject_an_active_entry(store, tmp_path, monkeypatch):
    main = "The widget service deploys from the main branch with make deploy."
    old = engine.store_entry("Widget service deploy", main, valid_from=EARLIER)
    new = engine.store_entry("Widget service deploy rule", RULE, valid_from=EARLIER)
    engine.supersede(old, new)
    triage(monkeypatch, ["replacement"], [candidate(main, tags=["widget"])])
    [result] = distill.distill([str(roster_capture(tmp_path))])
    assert result["action"] == "matched-retired"
    assert engine._parse_frontmatter(engine.get(new))["status"] == "active"
    assert engine.supersede(new, new) is False


def test_triage_never_retires_what_a_person_corrected(store, monkeypatch):
    corrected = engine.store_entry("Widget service deploy", RULE, importance=7, valid_from=EARLIER)
    triage(monkeypatch, ["replacement"])
    [kept] = distill.diff_against_store([candidate(
        "A peer stated the widget service deploys from the main branch with make deploy.")])
    assert "_supersedes" not in kept and "_append_to" not in kept
    assert engine._parse_frontmatter(engine.get(corrected))["status"] == "active"


def test_a_capture_distilled_late_is_retired_by_the_newer_fact(store, tmp_path, monkeypatch):
    newer = engine.store_entry("Widget service deploy", RULE, valid_from="2026-10-14T20:00:00Z")
    triage(monkeypatch, ["replacement"], [candidate(
        "The widget service deploys from the main branch with make deploy.", tags=["widget"])])
    [result] = distill.distill([str(roster_capture(tmp_path))])
    assert result["action"] == "stored-retired"
    assert engine._parse_frontmatter(engine.get(newer))["status"] == "active"
    assert engine._parse_frontmatter(engine.get(result["id"]))["superseded_by"] == newer
    assert [h["id"] for h in engine.search("widget service deploy", limit=5)] == [newer]


def test_one_entry_is_retired_once_per_capture(store, tmp_path, monkeypatch):
    old = engine.store_entry("Widget service deploy", RULE, valid_from=EARLIER)
    triage(monkeypatch, ["replacement", "replacement"])
    kept = distill.diff_against_store([
        candidate("The widget service deploys from the main branch with make deploy."),
        candidate("The widget service deploys from the trunk branch with make deploy."),
    ], "2026-10-14T16:30:00Z")
    assert [k.get("_supersedes") for k in kept] == [old, None]


def test_a_correction_keeps_what_it_retired(store, tmp_path, monkeypatch):
    old = engine.store_entry("Widget service deploy", RULE, valid_from=EARLIER)
    monkeypatch.setattr(distill, "corrections_from_capture", lambda path: [
        {"title": "Widget service deploy branch", "content": "Deploys of the widget service use main now.",
         "tags": ["widget"], "_supersedes": old}])
    triage(monkeypatch, ["replacement"], [{"title": "Widget service deploy", "tags": ["widget"],
                                           "content": "Orchard sprinklers at dawn; harbor tides near noon."}])
    results = distill.distill([str(roster_capture(tmp_path))])
    correction = next(r for r in results if r.get("old_id") == old)
    assert engine._parse_frontmatter(engine.get(old))["superseded_by"] == correction["id"]
    assert [r["action"] for r in results].count("superseded") == 1


def test_a_task_fact_never_closes_an_idea(store, monkeypatch):
    engine.store_entry("Orchard sprinkler timer idea",
                       "Maybe the orchard sprinklers should run on a dawn timer.", kind="idea", valid_from=EARLIER)
    triage(monkeypatch, ["replacement"])
    [kept] = distill.diff_against_store([{
        "title": "Orchard sprinkler timer", "lifespan": "task",
        "content": "The orchard sprinklers run on a dawn timer this week."}])
    assert "_supersedes" not in kept


def test_a_task_fact_in_the_overlap_band_is_triaged(store, monkeypatch):
    stale = engine.store_entry("Widget freeze", "The widget service deploy freeze holds until Friday noon.",
                               valid_to="2099-01-01", valid_from=EARLIER)
    triage(monkeypatch, ["replacement"])
    [kept] = distill.diff_against_store([{
        "title": "Release window", "lifespan": "task",
        "content": "The widget service deploy freeze lifted early, before Friday noon, after the patch."}])
    assert kept["_supersedes"] == stale
