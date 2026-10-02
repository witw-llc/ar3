"""Deterministic citation namespaces and retained-source validation; no entailment claims."""
import hashlib
import json

import pytest

import cli
import distill
import engine
import hygiene


def reference(store, text="SYNTHETIC garden proposal remains undecided.", source_id="synthetic-source", structured=False):
    raw = json.dumps({"records": [{"source_id": source_id, "text": text}]}).encode() if structured else text.encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = store / "sources" / f"{digest}.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(raw)
    ref = {"version": 1, "source_id": source_id, "sha256": digest,
           "snapshot": f"sources/{digest}.txt", "start": 0, "end": len(text),
           "quote_sha256": hashlib.sha256(text.encode()).hexdigest()}
    if structured:
        ref["record_index"] = 0
    return ref, path


def test_current_retrieval_named_retired_id_requires_history_flag_every_time(store, monkeypatch, capsys):
    old = engine.store_entry("Synthetic old garden", "SYNTHETIC blue garden decision.", kind="decision")
    new = engine.store_entry("Synthetic replacement", "SYNTHETIC green garden replaces blue.", kind="decision")
    engine.supersede(old, new)
    monkeypatch.setattr(engine, "_call_llm", lambda *a, **k: f"Synthetic history [{old}].")
    monkeypatch.setenv("K7E_SUMMARIZE_COMMAND", "synthetic-not-executed")
    for repair in [lambda: None, engine.reindex, lambda: hygiene.run_audit(fix=True)]:
        repair()
        assert engine.search(old) == []
        assert engine.recall(old) == (None, [])
        assert engine.search(old, include_superseded=True, include_archive=True)[0]["id"] == old
        assert engine.recall(old, include_superseded=True, include_archive=True)[1][0]["status"] == "superseded"
        assert engine.search(old, include_superseded=True, active_only=True) == []
        assert cli.main(["recall", old]) == 0
        assert "No relevant knowledge found." in capsys.readouterr().out
        assert cli.main(["recall", old, "--include-superseded", "--include-archive"]) == 0
        assert "Synthetic history" in capsys.readouterr().out
    assert engine._parse_frontmatter(engine.get(old))["status"] == "superseded"
    candidate = {"title": "Synthetic peer restatement", "content": f"{old}: SYNTHETIC blue garden decision repeated."}
    assert all(item.get("_append_to") != old for item in distill.diff_against_store([candidate]))


def test_backing_file_status_gates_recall_even_if_index_is_stale(store, monkeypatch):
    old = engine.store_entry("Synthetic garden", "SYNTHETIC stale garden claim.")
    new = engine.store_entry("Synthetic replacement", "SYNTHETIC replacement garden claim.")
    engine.supersede(old, new)
    conn = engine._connect()
    conn.execute("UPDATE nodes SET status='active' WHERE id=?", (old,))
    conn.commit();conn.close()
    monkeypatch.setattr(engine, "_call_llm", lambda *a, **k: "Synthetic answer.")
    assert engine.search(old) == []
    assert engine.recall(old) == (None, [])
    assert hygiene.index_disagreement()
    hygiene.run_audit(fix=True)
    assert not hygiene.index_disagreement()
    assert engine.search(old) == []


def test_source_handles_resolve_original_id_span_snapshot_and_unknown_origin(store, monkeypatch):
    ref, _ = reference(store, structured=True)
    nid = engine.store_entry("Synthetic garden", "SYNTHETIC possible garden.", kind="idea", source_refs=[ref])
    monkeypatch.setattr(engine, "_call_llm", lambda *a, **k: f"Synthetic possibility [{nid}] [SRC-1].")
    _, entries = engine.recall("garden", include_archive=True)
    entry = entries[0]
    assert entry["source_refs"] == [ref]
    assert not entry["source_ref_errors"] and not entry["citation_errors"]
    source = next(c for c in entry["resolved_citations"] if c["namespace"] == "original_source")
    assert source["source_id"] == "synthetic-source" and source["origin"] == "unknown"
    assert source["span"]["namespace"] == "unicode_chars" and source["span"]["end"] == ref["end"]
    assert source["snapshot"] == {"namespace": "snapshot", "path": ref["snapshot"], "sha256": ref["sha256"]}
    assert entry["resolved_citations"][0]["namespace"] == "entry"


@pytest.mark.parametrize("token", ["SRC-999", "K7E-999-99999", "synthetic-source", "span:0:999", "sources/invented.txt", "SRC-1 span 0:999"])
def test_generated_ids_spans_paths_do_not_enter_resolved_reference_namespace(store, monkeypatch, token):
    ref, _ = reference(store)
    nid = engine.store_entry("Synthetic garden", "SYNTHETIC garden proposal.", kind="idea", source_refs=[ref])
    monkeypatch.setattr(engine, "_call_llm", lambda *a, **k: f"Synthetic claim [{nid}] [SRC-1] [{token}].")
    _, entries = engine.recall("garden", include_archive=True)
    assert any("unsupported citation" in error for error in entries[0]["citation_errors"])
    assert [c["handle"] for c in entries[0]["resolved_citations"] if c["namespace"] == "original_source"] == ["SRC-1"]


@pytest.mark.parametrize("mutation", ["missing", "tampered", "path", "range", "id", "version", "record"])
def test_malformed_or_unavailable_sources_are_flagged_and_get_no_handle(store, monkeypatch, mutation):
    ref, path = reference(store, structured=True)
    if mutation == "missing": path.unlink()
    elif mutation == "tampered": path.write_text("SYNTHETIC changed bytes.")
    elif mutation == "path": ref["snapshot"] = "../outside.txt"
    elif mutation == "range": ref["end"] = 99999
    elif mutation == "id": ref["source_id"] = "synthetic-invented"
    elif mutation == "version": ref["version"] = True
    elif mutation == "record": ref["record_index"] = -1
    nid = engine.store_entry("Synthetic garden", "SYNTHETIC possible garden.", kind="idea", source_refs=[ref])
    monkeypatch.setattr(engine, "_call_llm", lambda *a, **k: f"Synthetic claim [{nid}] [SRC-1].")
    _, entries = engine.recall("garden", include_archive=True)
    assert entries[0]["source_ref_errors"]
    assert entries[0]["citations"] == []
    assert all(c["namespace"] == "entry" for c in entries[0]["resolved_citations"])
    assert any("unsupported citation [SRC-1]" in error for error in entries[0]["citation_errors"])


def test_validation_budget_flags_without_erasing_retained_provenance(store, monkeypatch):
    ref, _ = reference(store)
    nid = engine.store_entry("Synthetic garden", "SYNTHETIC garden proposal.", kind="idea", source_refs=[ref])
    monkeypatch.setattr(engine, "SOURCE_VALIDATION_BYTES", 1)
    monkeypatch.setattr(engine, "_call_llm", lambda *a, **k: f"Synthetic answer [{nid}].")
    _, entries = engine.recall("garden", include_archive=True)
    assert entries[0]["source_refs"] == [ref] and entries[0]["citations"] == []
    assert "budget" in entries[0]["source_ref_errors"][0]


def test_cli_marks_unverified_citations_and_prints_only_resolved_sources(store, monkeypatch, capsys):
    ref, _ = reference(store)
    nid = engine.store_entry("Synthetic garden", "SYNTHETIC possible garden.", source_refs=[ref])
    monkeypatch.setenv("K7E_SUMMARIZE_COMMAND", "synthetic-not-executed")
    monkeypatch.setattr(engine, "_call_llm", lambda *a, **k: f"Synthetic claim [{nid}] [SRC-1] [SRC-999].")
    assert cli.main(["recall", "garden", "--include-archive"]) == 0
    result = capsys.readouterr()
    assert "Citation validation warnings:" in result.err
    assert "Validated original sources: [SRC-1] (sources/" in result.out
    assert "[SRC-999]" not in result.out.split("Validated original sources:", 1)[1]


def test_source_id_equal_to_entry_id_has_distinct_namespace(store, monkeypatch):
    nid = engine.store_entry("Synthetic garden", "SYNTHETIC possible garden.")
    ref, _ = reference(store, source_id=nid)
    engine.append_entry(nid, "Source Claims", "SYNTHETIC added source.", source_refs=[ref])
    monkeypatch.setattr(engine, "_call_llm", lambda *a, **k: f"Synthetic statement [{nid}] [SRC-1].")
    _, entries = engine.recall("garden", include_archive=True)
    assert [c["namespace"] for c in entries[0]["resolved_citations"]] == ["entry", "original_source"]


def test_unclosed_and_missing_citations_are_explicitly_flagged(store, monkeypatch):
    engine.store_entry("Synthetic garden", "SYNTHETIC possible garden.")
    monkeypatch.setattr(engine, "_call_llm", lambda *a, **k: "Synthetic answer [SRC-1")
    _, entries = engine.recall("garden", include_archive=True)
    assert "unclosed citation handle" in entries[0]["citation_errors"]
    assert "answer has no validated citations" in entries[0]["citation_errors"]


def test_cli_search_requires_history_flag_for_retired_exact_id(store, capsys):
    old = engine.store_entry("Synthetic old", "SYNTHETIC old shelf decision.")
    new = engine.store_entry("Synthetic new", "SYNTHETIC new shelf decision.")
    engine.supersede(old, new)
    assert cli.main(["search", old, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert cli.main(["search", old, "--json", "--include-superseded"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == old


def test_reference_count_budget_and_missing_original_citations_are_flagged(store, monkeypatch):
    ref, _ = reference(store)
    nid = engine.store_entry("Synthetic garden", "SYNTHETIC garden possibility.", source_refs=[ref, {**ref, "source_id": "synthetic-second"}])
    monkeypatch.setattr(engine, "SOURCE_VALIDATION_REFS", 1)
    monkeypatch.setattr(engine, "_call_llm", lambda *a, **k: f"Synthetic answer [{nid}].")
    _, entries = engine.recall("garden", include_archive=True)
    assert len(entries[0]["citations"]) == 1
    assert "reference budget" in entries[0]["source_ref_errors"][0]
    assert "answer has no validated original-source citations" in entries[0]["citation_errors"]


def test_snapshot_symlink_outside_store_is_not_read(store, tmp_path, monkeypatch):
    ref, path = reference(store)
    outside = tmp_path.parent / "synthetic-citation-outside.txt"
    outside.write_text("SYNTHETIC outside-store text.")
    path.unlink()
    path.symlink_to(outside)
    nid = engine.store_entry("Synthetic garden", "SYNTHETIC garden proposal.", source_refs=[ref])
    monkeypatch.setattr(engine, "_call_llm", lambda *a, **k: f"Synthetic answer [{nid}].")
    _, entries = engine.recall("garden", include_archive=True)
    assert "escapes the store" in entries[0]["source_ref_errors"][0]
    assert entries[0]["citations"] == []


def test_structured_dates_and_unicode_spans_are_validated(store, monkeypatch):
    text = "SYNTHETIC α garden decision."
    record = {"source_id": "synthetic-greek", "text": text, "stated_at": "2026-01-01", "effective_at": "2026-02-01"}
    raw = json.dumps({"records": [record]}, ensure_ascii=False).encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = store / "sources" / f"{digest}.txt"
    path.parent.mkdir(exist_ok=True);path.write_bytes(raw)
    ref = {"source_id": record["source_id"], "sha256": digest, "snapshot": f"sources/{digest}.txt", "record_index": 0,
           "start": text.index("α"), "end": len(text), "quote_sha256": hashlib.sha256(text[text.index("α"):].encode()).hexdigest(), "stated_at": record["stated_at"], "effective_at": record["effective_at"]}
    nid = engine.store_entry("Synthetic garden", "SYNTHETIC garden decision.", kind="decision", source_refs=[ref])
    monkeypatch.setattr(engine, "_call_llm", lambda *a, **k: f"Synthetic decision [{nid}] [SRC-1].")
    _, entries = engine.recall("garden", include_archive=True)
    assert entries[0]["citations"][0]["quote_excerpt"] == text[text.index("α"):]
    assert entries[0]["citations"][0]["effective_at"] == "2026-02-01"
    assert not entries[0]["source_ref_errors"]
    body = engine.get(nid)
    body = engine._set_frontmatter_key(body, "source_refs", json.dumps([{**ref, "effective_at": "2026-09-01"}]))
    engine._node_path(nid).write_text(body)
    assert "effective_at disagrees" in engine.recall("garden", include_archive=True)[1][0]["source_ref_errors"][0]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("historical", [False, True])
def test_decomposed_exact_and_ranked_hits_merge_in_both_orders(store, monkeypatch, reverse, historical):
    nid = engine.store_entry("Synthetic garden", "SYNTHETIC garden plan.")
    if historical:
        replacement = engine.store_entry("Synthetic replacement", "SYNTHETIC replacement plan.")
        engine.supersede(nid, replacement)
    queries = [nid, "garden"]
    if reverse:
        queries.reverse()
    monkeypatch.setattr(engine, "_decompose_queries", lambda text: queries)
    monkeypatch.setattr(engine, "_call_llm", lambda *a, **k: f"Synthetic answer [{nid}].")
    _, entries = engine.recall("SYNTHETIC extended query " * 30, include_superseded=historical)
    assert [entry["id"] for entry in entries].count(nid) == 1
    assert next(entry for entry in entries if entry["id"] == nid)["status"] == ("superseded" if historical else "active")


def test_idless_snapshot_requires_content_addressed_generated_locator(store):
    raw = json.dumps({"records": [{"text": "SYNTHETIC undecided garden."}]}).encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = store / "sources" / f"{digest}.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(raw)
    ref = {"source_id": f"sha256:{digest}#record-0", "sha256": digest,
           "snapshot": f"sources/{digest}.txt", "record_index": 0, "start": 0, "end": 10,
           "quote_sha256": hashlib.sha256("SYNTHETIC ".encode()).hexdigest()}
    state = {"bytes": 0, "data": {}}
    assert engine._validate_source_ref(ref, state)["source_id"] == ref["source_id"]
    for fake in ["FABRICATED-ORIGINAL-ID", "synthetic/path#record-0", f"sha256:{digest}#record-1"]:
        with pytest.raises(ValueError, match="ID disagrees"):
            engine._validate_source_ref({**ref, "source_id": fake}, state)


def test_wikilinks_and_markdown_link_labels_are_not_forged_citations(store, monkeypatch):
    ref, _ = reference(store)
    nid = engine.store_entry('Synthetic garden', 'Synthetic garden option.', source_refs=[ref])
    monkeypatch.setattr(engine, '_call_llm', lambda *a, **k: f'Synthetic claim [[{nid}]] [SRC-1]; [reference](https://example.invalid).')
    _, entries = engine.recall('garden')
    assert not entries[0]['citation_errors']
    assert len(entries[0]['resolved_citations']) == 2


@pytest.mark.parametrize('hash_value', [None, '', '0' * 64, True, 42])
def test_missing_or_invalid_quote_hash_cannot_produce_a_validated_handle(store, monkeypatch, capsys, hash_value):
    ref, _ = reference(store)
    if hash_value is None:
        ref.pop('quote_sha256')
    else:
        ref['quote_sha256'] = hash_value
    ref['end'] = 10
    nid = engine.store_entry('Synthetic garden', 'A synthetic garden option.', kind='idea', source_refs=[ref])
    monkeypatch.setenv('K7E_SUMMARIZE_COMMAND', 'synthetic-not-executed')
    monkeypatch.setattr(engine, '_call_llm', lambda *a, **k: f'Synthetic claim [{nid}] [SRC-1].')
    _, entries = engine.recall('garden', include_archive=True)
    assert entries[0]['source_refs'] == [ref]
    assert not entries[0]['citations'] and entries[0]['source_ref_errors']
    assert not any(c['namespace'] == 'original_source' for c in entries[0]['resolved_citations'])
    assert cli.main(['recall', 'garden', '--include-archive']) == 0
    output = capsys.readouterr()
    assert 'Validated original sources' not in output.out
    assert 'Citation validation warnings' in output.err
