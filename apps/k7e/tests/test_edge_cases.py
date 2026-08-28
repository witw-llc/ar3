"""Edge case tests — unicode, injection, empty, huge content."""
import pytest
import engine


# Windows caps a single environment variable at 32,767 characters, and pytest
# puts the node ID in PYTEST_CURRENT_TEST on every setup and teardown. A
# parametrised case whose value IS its node ID therefore takes itself out
# before its own assertions run — two errors per case, from pytest's
# bookkeeping rather than from anything under test, and invisible on POSIX
# where no such cap exists. `pytest.param(..., id="short-name")` is the fix.
MAX_NODE_ID_CHARS = 4000


def test_no_node_id_could_overflow_a_windows_environment_variable(request):
    """Complete only on a full run: `-k` narrows `session.items` too."""
    oversize = [
        f"{item.nodeid[:100]}... ({len(item.nodeid)} chars)"
        for item in request.session.items
        if len(item.nodeid) > MAX_NODE_ID_CHARS
    ]
    assert not oversize, (
        "a node ID this long is set into PYTEST_CURRENT_TEST and Windows "
        "refuses it:\n" + "\n".join(oversize)
    )


class TestEdgeCases:
    @pytest.mark.parametrize("title,content,should_store", [
        ("Normal Title", "Normal content", True),
        ("Unicode: 🎉🚀", "Emoji content 🌍", True),
        ("日本語タイトル", "Japanese content here", True),
        ("Title with: colons", "YAML-breaking colons: in: content", True),
        # An explicit id, because pytest puts the node ID in
        # PYTEST_CURRENT_TEST and Windows caps one environment variable at
        # 32,767 characters. Without it the parameter IS the node ID, and
        # pytest's own bookkeeping raises at setup and again at teardown —
        # while the assertions here are fine.
        pytest.param("Title", "x" * 50000, True, id="very-long-content"),
        ("A" * 200, "very long title", True),
    ])
    def test_store_and_retrieve(self, store, title, content, should_store):
        node_id = engine.store_entry(title, content, tags=["edge"])
        text = engine.get(node_id)
        assert node_id in text
        if len(content) < 1000:
            assert content in text

    def test_empty_content(self, store):
        node_id = engine.store_entry("Empty", "", tags=["test"])
        text = engine.get(node_id)
        assert "Empty" in text

    def test_newlines_in_content(self, store):
        content = "Line 1\nLine 2\n\nLine 4"
        node_id = engine.store_entry("Multiline", content, tags=["test"])
        text = engine.get(node_id)
        assert "Line 1" in text
        assert "Line 4" in text

    def test_special_chars_in_tags(self, store):
        node_id = engine.store_entry("Tagged", "content", tags=["c++", "node.js", "tcp-ip"])
        nodes = engine.list_nodes(tag="c++")
        assert len(nodes) >= 1

    def test_search_with_special_chars(self, store):
        engine.store_entry("Flag Entry", "Use --remote-debugging-port=9222", tags=["chrome"])
        results = engine.search("--remote-debugging-port")
        assert len(results) >= 1
