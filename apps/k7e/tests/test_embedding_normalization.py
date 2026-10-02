"""Offline regressions for the versioned embedding-only view."""

import hashlib
import io
import json
from types import SimpleNamespace

import pytest

import embeddings
import engine


TEXT_VERSION = "title-body-500-reserved-headings-v2"
RESERVED_HEADINGS = ("Verified Protocol", "Edge Cases", "False Paths", "History")


def persisted(body, title="Synthetic note"):
    return f"---\ntitle: {title}\nstatus: active\n---\n{body}"


@pytest.mark.parametrize("heading", RESERVED_HEADINGS)
def test_exact_reserved_headings_are_omitted_but_their_content_survives(heading):
    body = (
        f"## {heading}\n\nAuthored claim.\n\n"
        f"## {heading}\n\n* 2042-03-04: Keep the complete history entry.\n"
    )
    assert engine._embedding_text(persisted(body)) == (
        "Synthetic note Authored claim.\n"
        "* 2042-03-04: Keep the complete history entry."
    )


@pytest.mark.parametrize("heading", RESERVED_HEADINGS)
@pytest.mark.parametrize("line", [
    " ## {}", "## {} ", "## {}\t", "## {}\u00a0", "##  {}",
    "##\t{}", "# {}", "### {}", "## {} notes", "## {} ##", "> ## {}",
])
def test_heading_matching_does_not_trim_or_accept_near_misses(heading, line):
    authored = line.format(heading)
    assert engine._embedding_text(persisted(authored)) == f"Synthetic note {authored}"


def test_custom_case_changed_and_inline_headings_survive():
    body = "## Custom section\n## history\n## VERIFIED PROTOCOL\nKeep ## Edge Cases inline."
    assert engine._embedding_text(persisted(body)) == f"Synthetic note {body}"


def test_only_whitespace_only_lines_are_removed_outside_fences():
    body = "\n \n\t\n\u2003\n  Keep indentation.  \n \n\tKeep tabs.\t\n\n## Custom\n"
    assert engine._embedding_text(persisted(body)) == (
        "Synthetic note   Keep indentation.  \n\tKeep tabs.\t\n## Custom"
    )


def test_title_is_parsed_from_source_without_body_normalization():
    title = "  History / ## Verified Protocol / caf\u00e9  "
    assert engine._embedding_text(persisted("## History\n\nA claim.", title)) == (
        "History / ## Verified Protocol / caf\u00e9 A claim."
    )
    assert engine._embedding_text("## History\n\nA claim.") == " A claim."


def test_reserved_headings_and_blank_lines_can_produce_an_empty_body():
    body = "\n \n".join(f"## {name}" for name in RESERVED_HEADINGS)
    assert engine._embedding_text(persisted(body)) == "Synthetic note "


@pytest.mark.parametrize("char,length,indent,close_length,close_indent", [
    ("`", 3, "", 3, ""),
    ("`", 4, "   ", 6, "  "),
    ("`", 7, " ", 7, "   "),
    ("~", 3, " ", 5, "   "),
    ("~", 4, "   ", 4, ""),
    ("~", 7, "  ", 8, " "),
])
def test_fence_state_preserves_every_line_until_a_matching_sufficient_closer(
        char, length, indent, close_length, close_indent):
    other = "~" if char == "`" else "`"
    code = [
        f"{indent}{char * length}markdown",
        "",
        " \t ",
        "## History",
        other * (length + 2),
        "## Edge Cases",
        char * (length - 1),
        "## False Paths",
        f"    {char * (length + 2)}",
        "## Verified Protocol",
        f"{close_indent}{char * close_length} trailing text",
        "",
        "## History",
        f"{close_indent}{char * close_length} \t ",
    ]
    body = "\n".join(["## Verified Protocol", "Before.", *code, "", "## History", "After."])
    expected = "\n".join(["Before.", *code, "After."])
    assert engine._embedding_text(persisted(body)) == f"Synthetic note {expected}"


@pytest.mark.parametrize("opening", [
    "``python", "~~text", "    ```", " \t  ~~~", "> ```", "text ```",
    "\t```", " \t~~~", "  \t```", "   \t~~~", "\u00a0```", "\u2003~~~",
])
def test_short_indented_quoted_and_inline_markers_do_not_open_fences(opening):
    body = f"{opening}\n\n## History\n \t \nKeep this."
    assert engine._embedding_text(persisted(body)) == f"Synthetic note {opening}\nKeep this."


@pytest.mark.parametrize("char", ["`", "~"])
@pytest.mark.parametrize("indent", ["\t", " \t", "  \t", "   \t", "\u00a0", "\u2003"])
def test_tab_or_unicode_whitespace_indented_markers_do_not_close_fences(char, indent):
    marker = char * 3
    code = [marker, "", f"{indent}{marker}", "## History", " \t ", marker]
    body = "\n".join([*code, "", "## History", "After."])
    expected = "\n".join([*code, "After."])
    assert engine._embedding_text(persisted(body)) == f"Synthetic note {expected}"


@pytest.mark.parametrize("opening", [
    "```has`tick", "````lang```", " ``` `", "   ````lang`x",
])
def test_backticks_in_info_suffix_prevent_a_backtick_fence_from_opening(opening):
    body = f"{opening}\n\n## History\n \t \nKeep this."
    assert engine._embedding_text(persisted(body)) == f"Synthetic note {opening}\nKeep this."


@pytest.mark.parametrize("opening,closing", [
    ("~~~lang`tick", "~~~"),
    (" ~~~~```", "~~~~"),
    ("  ~~~~~~\t`lang`", "~~~~~~"),
])
def test_tilde_fence_info_can_contain_backticks(opening, closing):
    code = [opening, "", "## History", " \t ", closing]
    body = "\n".join(["## Verified Protocol", *code, "", "## History", "After."])
    expected = "\n".join([*code, "After."])
    assert engine._embedding_text(persisted(body)) == f"Synthetic note {expected}"


@pytest.mark.parametrize("char", ["`", "~"])
@pytest.mark.parametrize("suffix", ["\u00a0", "\u2003", " # comment"])
def test_closer_suffix_must_contain_only_spaces_or_tabs(char, suffix):
    marker = char * 3
    code = [marker, f"{marker}{suffix}", "", "## History", " \t ", marker]
    body = "\n".join([*code, "", "## History", "After."])
    expected = "\n".join([*code, "After."])
    assert engine._embedding_text(persisted(body)) == f"Synthetic note {expected}"


@pytest.mark.parametrize("opening,other", [
    ("```python", "~~~~~~~"),
    ("~~~~markdown", "```````"),
    ("   `````", "~~~~~~~"),
])
def test_unclosed_fences_keep_reserved_headings_and_trailing_whitespace(opening, other):
    code = [opening, "", "## History", " \t ", other, "", "## Edge Cases", "", " \t"]
    body = "\n".join(["## Verified Protocol", "", *code])
    expected = "\n".join(code)
    assert engine._embedding_text(persisted(body)) == f"Synthetic note {expected}"


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085", "\v", "\f"])
def test_unicode_and_control_separators_inside_code_are_preserved_literally(separator):
    code = f"```text\nbefore{separator}## History{separator}after\n\n \t \n```"
    body = f"## Verified Protocol\n{code}\n\n## History\nAfter."
    assert engine._embedding_text(persisted(body)) == f"Synthetic note {code}\nAfter."


@pytest.mark.parametrize("trailing", ["\n", "\n\n", "\n\n \t\n\n"])
def test_trailing_blank_lines_in_an_unclosed_fence_are_preserved(trailing):
    code = f"```text\nLiteral content.\n## History{trailing}"
    body = f"## Verified Protocol\n\n{code}"
    assert engine._embedding_text(persisted(body)) == f"Synthetic note {code}"


def test_unicode_cutoff_counts_characters_after_removing_boilerplate():
    boilerplate = "## Verified Protocol\n\n \t\n## History\n\n" * 30
    body = boilerplate + "\u00e9" * 249 + "\n\n## Edge Cases\n \n" + "\U00010348" * 249 + "\u6771OMITTED"
    expected = "\u00e9" * 249 + "\n" + "\U00010348" * 249 + "\u6771"
    assert len(expected) == 500
    assert len(expected.encode("utf-8")) > 500
    assert engine._embedding_text(persisted(body)) == f"Synthetic note {expected}"


@pytest.fixture
def embedding_transport(store, monkeypatch):
    monkeypatch.setenv("K7E_EMBEDDINGS", "openai")
    monkeypatch.setenv("EMBED_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("K7E_EMBED_DIMENSIONS", "3")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-runtime-key")
    calls = []

    def request(req, timeout):
        assert req.full_url == "https://api.openai.com/v1/embeddings"
        payload = json.loads(req.data)
        calls.append((payload["input"], timeout))
        return io.BytesIO(json.dumps({
            "model": "text-embedding-3-small",
            "data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}],
        }).encode())

    monkeypatch.setattr(embeddings.urllib.request, "build_opener",
                        lambda *args: SimpleNamespace(open=request))
    monkeypatch.setattr(embeddings.urllib.request, "urlopen", request)
    return calls


def snapshot(node_id):
    conn = engine._connect()
    try:
        indexed = conn.execute(
            "SELECT f.content, n.embedding_input_hash FROM nodes n "
            "JOIN nodes_fts f ON f.rowid = n.rowid WHERE n.id = ?", (node_id,),
        ).fetchone()
        cached = conn.execute(
            "SELECT vector, provider, model, dimensions, text_hash, text_version, updated_at "
            "FROM embeddings WHERE node_id = ?", (node_id,),
        ).fetchone()
        return indexed, cached
    finally:
        conn.close()


CONTENT = (
    "## Verified Protocol\n\nKeep caf\u00e9 / rollout.\n\n"
    "## Edge Cases\n\nGuard shell syntax.\n\n"
    "## False Paths\n\nReject unsupported mode.\n\n"
    "## History\n\n* 2042-03-04: Preserve the rationale.\n\n"
    "## Custom detail\n\n```markdown\n## History\n\n \t \n~~~\n```\n\nFinish after code."
)
NORMALIZED = (
    "Keep caf\u00e9 / rollout.\nGuard shell syntax.\nReject unsupported mode.\n"
    "* 2042-03-04: Preserve the rationale.\n## Custom detail\n"
    "```markdown\n## History\n\n \t \n~~~\n```\nFinish after code."
)


def test_first_write_and_reindexes_use_the_same_view_without_rewriting_source_or_fts(embedding_transport):
    title = "  Synthetic History / ## Verified Protocol  "
    node_id = engine.store_entry(title, CONTENT)
    path = engine._node_path(node_id)
    source = path.read_bytes()
    body = engine._extract_body(source.decode("utf-8"))
    expected = f"{title.strip()} {NORMALIZED}"
    expected_hash = hashlib.sha256(expected.encode()).hexdigest()
    before, cached = snapshot(node_id)
    assert before == (CONTENT, expected_hash)
    assert cached is None
    assert embedding_transport == []

    assert engine.process_pending_embeddings() == 1
    assert embedding_transport == [(expected, engine.EMBED_TIMEOUT)]
    indexed, original_cache = snapshot(node_id)
    assert indexed == before
    assert original_cache[4:6] == (expected_hash, TEXT_VERSION)
    assert embeddings.TEXT_VERSION == TEXT_VERSION
    assert path.read_bytes() == source

    for _ in range(2):
        engine.reindex()
        assert snapshot(node_id) == ((body, expected_hash), original_cache)
        assert engine.embedding_coverage() == {"total": 1, "current": 1, "pending": 0}
        assert engine.process_pending_embeddings() == 0
        assert embedding_transport == [(expected, engine.EMBED_TIMEOUT)]
        assert path.read_bytes() == source

    engine.reindex(embeddings=True)
    assert embedding_transport == [(expected, engine.EMBED_TIMEOUT)] * 2
    assert snapshot(node_id)[0] == (body, expected_hash)
    assert path.read_bytes() == source
    conn = engine._connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM nodes_fts WHERE nodes_fts MATCH ?", ('"Verified Protocol"',),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM nodes_fts WHERE nodes_fts MATCH ?", ("rationale",),
        ).fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize("legacy_hash", ["raw-body", "normalized-body"])
def test_v1_cache_is_invalidated_and_refreshed_only_once(embedding_transport, legacy_hash):
    title = "Synthetic note"
    node_id = engine.store_entry(title, CONTENT)
    path = engine._node_path(node_id)
    source = path.read_bytes()
    body = engine._extract_body(source.decode("utf-8"))
    expected = f"{title} {NORMALIZED}"
    expected_hash = hashlib.sha256(expected.encode()).hexdigest()
    old_input = f"{title} {body[:500]}" if legacy_hash == "raw-body" else expected
    old_hash = hashlib.sha256(old_input.encode()).hexdigest()
    selected = embeddings.space()
    conn = engine._connect()
    try:
        conn.execute("UPDATE nodes SET embedding_input_hash = ? WHERE id = ?", (old_hash, node_id))
        conn.execute(
            "INSERT INTO embeddings "
            "(node_id, vector, model, updated_at, provider, dimensions, text_hash, text_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (node_id, engine._pack_vector([0.0, 1.0, 0.0]), selected.model,
             "2042-03-04", selected.provider, 3, old_hash, "title-body-500-v1"),
        )
        conn.execute("DELETE FROM pending_embeddings WHERE node_id = ?", (node_id,))
        conn.commit()
        assert engine._valid_current_vector_ids(conn, selected) == set()
    finally:
        conn.close()

    before, _ = snapshot(node_id)
    assert engine.embedding_coverage() == {"total": 1, "current": 0, "pending": 1}
    assert engine.pending_embedding_count() == 1
    assert embedding_transport == []
    assert engine.process_pending_embeddings() == 1
    assert embedding_transport == [(expected, engine.EMBED_TIMEOUT)]
    indexed, refreshed_cache = snapshot(node_id)
    assert indexed == (before[0], expected_hash)
    assert refreshed_cache[0] == engine._pack_vector([1.0, 0.0, 0.0])
    assert refreshed_cache[4:6] == (expected_hash, TEXT_VERSION)
    assert engine.embedding_coverage() == {"total": 1, "current": 1, "pending": 0}
    assert engine.process_pending_embeddings() == 0
    assert snapshot(node_id) == (indexed, refreshed_cache)
    assert path.read_bytes() == source

    for _ in range(2):
        engine.reindex()
        assert snapshot(node_id) == ((body, expected_hash), refreshed_cache)
        assert engine.process_pending_embeddings() == 0
        assert embedding_transport == [(expected, engine.EMBED_TIMEOUT)]
        assert path.read_bytes() == source
