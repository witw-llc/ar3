"""Edge case tests — unicode, injection, empty, huge content."""
import pytest
import engine


class TestEdgeCases:
    @pytest.mark.parametrize("title,content,should_store", [
        ("Normal Title", "Normal content", True),
        ("Unicode: 🎉🚀", "Emoji content 🌍", True),
        ("日本語タイトル", "Japanese content here", True),
        ("Title with: colons", "YAML-breaking colons: in: content", True),
        ("Title", "x" * 50000, True),  # very long content
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
