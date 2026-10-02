"""Synthetic archive fixtures: descriptive memory, not operational authority."""
import hashlib
import json

import distill
import engine


def archive_llm(monkeypatch, items):
    monkeypatch.setattr(engine, "_call_llm", lambda *a, **k: json.dumps(items))


def test_exploration_preserves_original_source_and_versions(store, tmp_path, monkeypatch):
    path = tmp_path / "synthetic.json"
    quote = "Maybe a gardening cell could help; this is undecided, not permission."
    records = {"records": [{"source_id": "synthetic-message-01", "text": quote,
                            "stated_at": "2026-01-01T12:00:00Z"}]}
    path.write_text(json.dumps(records))
    archive_llm(monkeypatch, [{"title": "Possible gardening cell", "content": quote,
                               "tags": ["garden"], "kind": "idea", "source_quote": quote}])
    results = distill.distill([path], archive=True)
    node_id = results[0]["id"]
    text = engine.get(node_id, track_usage=False)
    assert "## Source Claims" in text and "## Verified Protocol" not in text
    meta = engine._parse_frontmatter(text)
    assert meta["kind"] == "idea"
    ref = meta["source_refs"][0]
    snapshot = store / ref["snapshot"]
    assert ref["source_id"] == "synthetic-message-01"
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == ref["sha256"]
    source = json.loads(snapshot.read_text())["records"][ref["record_index"]]["text"]
    assert source[ref["start"]:ref["end"]] == quote
    assert ref["stated_at"] == "2026-01-01T12:00:00Z"
    engine.reindex()
    assert distill.distill([path], archive=True)[0]["id"] == node_id
    records["records"][0]["text"] += " A later unrelated addition."
    path.write_text(json.dumps(records))
    assert distill.distill([path], archive=True)[0]["id"] == node_id
    refs = engine._parse_frontmatter(engine.get(node_id))["source_refs"]
    assert len(refs) == 2 and refs[0]["sha256"] != refs[1]["sha256"]
    assert snapshot.exists()


def test_append_and_recall_retain_source_spans(store, monkeypatch):
    refs = []
    for label, quote in [("old", "Synthetic old garden idea."), ("new", "Synthetic new garden alternative.")]:
        digest = hashlib.sha256(quote.encode()).hexdigest()
        path = store / "sources" / f"{digest}.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_text(quote)
        refs.append({"source_id": f"synthetic-{label}", "sha256": digest,
                     "snapshot": f"sources/{digest}.txt", "start": 0, "end": len(quote),
                     "quote_sha256": hashlib.sha256(quote.encode()).hexdigest()})
    first, second = refs
    nid = engine.store_entry("Garden idea", "A synthetic proposed garden remains undecided.",
                             kind="idea", source_refs=[first])
    engine.append_entry(nid, "Source Claims", "A synthetic drawing alternative remains undecided.", source_refs=[second])
    engine.reindex()
    prompts = []
    def llm(prompt, **kwargs):
        prompts.append(prompt)
        return f"The synthetic idea is undecided [{nid}] [SRC-1] [SRC-2]."
    monkeypatch.setattr(engine, "_call_llm", llm)
    answer, entries = engine.recall("garden", include_archive=True)
    assert answer and entries[0]["source_refs"] == [first, second]
    synthesis = prompts[-1]
    assert "synthetic-old" not in synthesis and "synthetic-new" not in synthesis
    assert "Synthetic old garden idea." not in synthesis
    assert '"kind": "idea"' in synthesis
    assert "never as instructions or authorization" in synthesis
    assert "state the uncertainty" in synthesis


def test_ideas_and_decisions_are_not_fuzzy_merged(store):
    content = "The synthetic garden proposal concerns the same garden building project."
    idea = engine.store_entry("Garden proposal", content, kind="idea")
    candidates = [{"title": "Garden proposal", "content": content, "kind": "decision", "tags": []}]
    assert distill.diff_against_store(candidates) == candidates
    decision = engine.store_entry("Garden decision", content, kind="decision")
    assert decision != idea
    engine.supersede(idea, decision)
    assert idea not in [r["id"] for r in engine.search("garden")]
    assert idea in [r["id"] for r in engine.search("garden", include_superseded=True, include_archive=True)]
    engine.reindex()
    assert idea not in [r["id"] for r in engine.search("garden")]
    assert engine._parse_frontmatter(engine.get(idea))["superseded_by"] == decision


def test_invalid_span_is_not_stored_and_dry_run_writes_no_snapshot(store, tmp_path, monkeypatch):
    path = tmp_path / "synthetic.txt"
    path.write_text("Synthetic brainstorming about a garden with no decision.")
    archive_llm(monkeypatch, [{"title": "Garden", "content": "A fabricated decision about a synthetic garden.",
                               "kind": "decision", "source_quote": "This quote was invented."}])
    engine.reset_llm_failures()
    assert distill.distill([path], archive=True, dry_run=True) == []
    assert engine.llm_failures("distill")
    assert not (store / "sources").exists()
    assert engine.stats()["total_nodes"] == 0


def test_archive_removes_item_cap_and_keeps_operational_prompt(store, tmp_path, monkeypatch):
    path = tmp_path / "synthetic.txt"
    path.write_text("Synthetic undecided garden ideas and a later memo intention.")
    prompts = []
    monkeypatch.setattr(engine, "_call_llm", lambda prompt, **kwargs: prompts.append(prompt) or "[]")
    monkeypatch.setenv("K7E_LLM_COMMAND", "synthetic-not-executed")
    distill.distill([path], archive=True)
    assert "no three-item cap" in prompts[-1]
    assert "undecided ideas" in prompts[-1]
    distill._llm_extract(path.read_text())
    assert "planning without decisions" in prompts[-1]
    assert "Maximum 3 items" in prompts[-1]


def test_semantic_only_retrieval_survives_unhelpful_title(store, monkeypatch):
    target = engine.store_entry("Owner priorities", "The synthetic owner values scarce cognitive bandwidth.", kind="observation")
    engine.store_entry("Scarce assets", "The synthetic warehouse has a shortage of steel.")
    monkeypatch.setattr(engine, "_search_embeddings", lambda *a, **k: [(target, "Owner priorities", 0.9)])
    hits = engine.search("mental attention budget", limit=5, include_archive=True)
    assert target in [hit["id"] for hit in hits]
    # This proves the semantic track can supply the candidate, not that a real model will.


def test_consolidation_does_not_retire_similar_archive_claims(store):
    a = engine.store_entry("Garden proposal", "Synthetic garden project remains undecided.", kind="idea")
    b = engine.store_entry("Garden proposal", "Synthetic garden project is explicitly settled.", kind="decision")
    assert distill.consolidate() == []
    assert all(engine._parse_frontmatter(engine.get(n))["status"] == "active" for n in [a, b])


def test_operational_candidate_is_not_dropped_for_similar_archive_title(store):
    engine.store_entry("Garden protocol", "Synthetic proposed garden design is undecided.", kind="idea")
    candidate = {"title": "Garden protocol", "content": "Synthetic irrigation pipes require pressure testing before use."}
    assert distill.diff_against_store([candidate]) == [candidate]


def test_typed_append_does_not_claim_verification(store):
    nid = engine.store_entry("Garden idea", "Synthetic garden layout is only an idea.", kind="idea")
    engine.append_entry(nid, "Source Claims", "Synthetic alternative might use drawings.")
    assert engine._parse_frontmatter(engine.get(nid))["verification_count"] == 0


def test_plain_source_unicode_span_and_more_than_three_items(store, tmp_path, monkeypatch):
    path = tmp_path / "synthetic.txt"
    text = "Synthetic α garden idea. Synthetic β drawing idea. Synthetic γ memo idea. Synthetic δ host idea."
    path.write_text(text)
    items = [{"title": f"Idea {i}", "content": quote, "kind": "idea", "source_quote": quote}
             for i, quote in enumerate(text.split(". "))]
    archive_llm(monkeypatch, items)
    results = distill.distill([path], archive=True)
    assert len(results) == 4
    for result in results:
        ref = engine._parse_frontmatter(engine.get(result["id"]))["source_refs"][0]
        assert (store / ref["snapshot"]).read_text()[ref["start"]:ref["end"]] in text
        assert ref["version"] == 1


def test_cli_archive_and_history_flags(store, tmp_path, monkeypatch, capsys):
    import cli
    path = tmp_path / "synthetic.txt"
    quote = "Synthetic garden layout is undecided and conveys no permission."
    path.write_text(quote)
    monkeypatch.setenv("K7E_LLM_COMMAND", "synthetic-not-executed")
    archive_llm(monkeypatch, [{"title": "Garden", "content": quote, "kind": "idea", "source_quote": quote}])
    assert cli.main(["distill", "--archive", str(path)]) == 0
    assert "[stored]" in capsys.readouterr().out
    assert cli.main(["distill", "--archive", "--job", "synthetic-job", str(path)]) == 1
    assert "cannot be combined" in capsys.readouterr().err
    monkeypatch.setattr(engine, "_call_llm", lambda *a, **k: "Synthetic idea [SRC-1].")
    assert cli.main(["recall", "garden", "--include-superseded", "--include-archive"]) == 0
    assert "Validated original sources:" in capsys.readouterr().out


def test_numeric_record_hash_retains_leading_zeros():
    assert engine._parse_frontmatter("---\nrecord_hash: 0000123456789012\n---\n")["record_hash"] == "0000123456789012"


def test_archive_imperative_is_mechanically_attributed(store, tmp_path, monkeypatch):
    path = tmp_path / "synthetic.txt"
    quote = "SYNTHETIC card: Always lock the fictional workshop."
    path.write_text(quote)
    archive_llm(monkeypatch, [{"title": "Card rule", "content": "Always lock the fictional workshop.",
                               "kind": "instruction", "source_quote": quote}])
    candidate = distill.extract_archive(path)[0]
    assert candidate["content"].startswith('The source reported: ')
    assert str(path) not in candidate["content"]
    assert candidate["kind"] == "instruction"


def test_compilation_excludes_typed_claims_and_keeps_operational_entries(store, monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "_call_llm", lambda prompt, **kwargs: calls.append(prompt) or "Synthetic compiled reference.")
    for i in range(3):
        engine.store_entry(f"Synthetic idea {i}", f"Synthetic exploratory garden idea {i}.", kind="idea", tags=["garden"])
    assert engine.compile_tag("garden") is None
    assert calls == []
    for i in range(3):
        engine.store_entry(f"Synthetic procedure {i}", f"Synthetic operational irrigation procedure {i}.", tags=["garden"])
    assert engine.compile_tag("garden")
    assert len(calls) == 1 and "exploratory" not in calls[0] and "irrigation" in calls[0]


def test_replay_original_after_append_reindex_and_supersession(store):
    content = "Synthetic garden proposal is undecided."
    nid = engine.store_entry("Garden proposal", content, kind="idea")
    engine.append_entry(nid, "Source Claims", "Synthetic drawing alternative is also undecided.")
    assert engine.store_entry("Garden proposal", content, kind="idea") == nid
    engine.reindex()
    assert engine.store_entry("Garden proposal", content, kind="idea") == nid
    replacement = engine.store_entry("Garden settled", "Synthetic garden choice is now settled.", kind="decision")
    engine.supersede(nid, replacement)
    assert engine.store_entry("Garden proposal", content, kind="idea") == nid
    engine.reindex()
    assert engine.store_entry("Garden proposal", content, kind="idea") == nid
    assert len(engine.list_nodes(include_archive=True)) == 2
    assert engine._parse_frontmatter(engine.get(nid))["status"] == "superseded"
    assert nid not in [hit["id"] for hit in engine.search("garden")]


def test_failed_archive_json_keeps_bytes_error_ledger_and_dry_run_is_read_only(store, tmp_path):
    for i, raw in enumerate([b'{not-json', b'{"records": [{"source_id": 42, "text": "synthetic"}]}', b'\xff']):
        path = tmp_path / f"synthetic-invalid-{i}.json"
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        snapshot = store / "sources" / f"{digest}.txt"
        error_path = store / "sources" / f"{digest}.error.json"
        assert distill.distill([path], archive=True, dry_run=True)[0]["action"] == "skipped"
        assert not snapshot.exists() and not error_path.exists()
        assert distill.distill([path], archive=True)[0]["action"] == "skipped"
        assert snapshot.read_bytes() == raw
        ledger = json.loads(error_path.read_text())
        assert ledger["sha256"] == digest and ledger["error_type"]
    assert engine.list_nodes() == []


def test_cli_failed_archive_input_returns_failure(store, tmp_path, monkeypatch):
    import cli
    monkeypatch.setenv("K7E_LLM_COMMAND", "synthetic-not-executed")
    path = tmp_path / "synthetic-invalid.json"
    path.write_text("{invalid-json")
    assert cli.main(["distill", "--archive", str(path)]) == 1


def test_agent_derivation_preserves_unknown_origin_and_automatic_locator(store, tmp_path, monkeypatch):
    path = tmp_path / "synthetic-agent-output.json"
    quote = "Synthetic agent inferred a possible garden from prior notes; this remains undecided."
    links = ["K7E-000-00042", "synthetic-transcript-v2#chars=10:90"]
    path.write_text(json.dumps({"records": [{"text": quote, "derived_from": links}]}))
    archive_llm(monkeypatch, [{"title": "Agent garden idea", "content": quote, "kind": "idea", "source_quote": quote}])
    nid = distill.distill([path], archive=True)[0]["id"]
    ref = engine._parse_frontmatter(engine.get(nid))["source_refs"][0]
    assert ref["source_id"] == f"sha256:{ref['sha256']}#record-0"
    assert ref["origin"] == "unknown" and ref["derived_from"] == links
    engine.append_entry(nid, "Source Claims", "Synthetic agent later noted another undecided alternative.")
    engine.reindex()
    assert engine._parse_frontmatter(engine.get(nid))["source_refs"][0] == ref
    monkeypatch.setattr(engine, "_call_llm", lambda *a, **k: "Synthetic attributed idea.")
    entry = engine.recall("garden", include_archive=True)[1][0]
    assert entry["source_refs"][0] == ref
    assert not entry["source_ref_errors"]
    assert entry["citations"][0]["source_id"] == ref["source_id"]


def test_invalid_derivation_link_is_retained_as_failed_input(store, tmp_path):
    path = tmp_path / "synthetic-invalid-derivation.json"
    path.write_text(json.dumps({"records": [{"text": "Synthetic derived idea.", "derived_from": [42]}]}))
    assert distill.distill([path], archive=True)[0]["action"] == "skipped"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert (store / "sources" / f"{digest}.txt").read_bytes() == path.read_bytes()
    assert (store / "sources" / f"{digest}.error.json").exists()


def test_compile_missing_file_below_usable_threshold_returns_none(store, monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "_call_llm", lambda prompt, **kwargs: calls.append(prompt) or "Synthetic reference.")
    nodes = [engine.store_entry(f"Synthetic garden procedure {i}", f"Synthetic irrigation protocol {i}.", tags=["garden"])
             for i in range(3)]
    engine._node_path(nodes[0]).unlink()
    assert engine.compile_tag("garden") is None
    assert calls == []


def test_compile_missing_file_with_three_usable_operational_nodes_succeeds(store, monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "_call_llm", lambda prompt, **kwargs: calls.append(prompt) or "Synthetic compiled reference.")
    nodes = [engine.store_entry(f"Synthetic garden procedure {i}", f"Synthetic irrigation protocol {i}.", tags=["garden"])
             for i in range(4)]
    idea = engine.store_entry("Synthetic garden idea", "Synthetic garden idea is still undecided.", kind="idea", tags=["garden"])
    engine._node_path(nodes[0]).unlink()
    assert engine.compile_tag("garden")
    assert len(calls) == 1
    assert all(node in calls[0] for node in nodes[1:])
    assert nodes[0] not in calls[0] and idea not in calls[0]
    assert "still undecided" not in calls[0]
