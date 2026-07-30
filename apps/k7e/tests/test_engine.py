"""Core engine tests — store, search, append, reindex, assets."""

import time
from pathlib import Path

import pytest
import engine


class TestStoreAndGet:
    def test_store_creates_file(self, store):
        node_id = engine.store_entry("Test Title", "Test content", tags=["testing"])
        path = engine._node_path(node_id)
        assert path.exists()

    def test_store_returns_k7e_id(self, store):
        node_id = engine.store_entry("Title", "Content")
        assert node_id.startswith("K7E-")
        parts = node_id.split("-")
        assert len(parts) == 3
        assert len(parts[1]) == 3
        assert len(parts[2]) == 5

    def test_store_sequential_ids(self, store):
        id1 = engine.store_entry("A", "a")
        id2 = engine.store_entry("B", "b")
        id3 = engine.store_entry("C", "c")
        assert id1 == "K7E-000-00001"
        assert id2 == "K7E-000-00002"
        assert id3 == "K7E-000-00003"

    def test_get_returns_content(self, store):
        engine.store_entry("My Title", "The knowledge")
        text = engine.get("K7E-000-00001")
        assert "My Title" in text
        assert "The knowledge" in text

    def test_get_missing_raises(self, store):
        with pytest.raises(FileNotFoundError):
            engine.get("K7E-999-99999")

    def test_node_has_frontmatter(self, store):
        engine.store_entry("Title", "Content", tags=["a", "b"], aliases=["alt"])
        text = engine.get("K7E-000-00001")
        assert "id: K7E-000-00001" in text
        assert "title: Title" in text
        assert "tags: [a, b]" in text
        assert "aliases: [alt]" in text

    def test_node_has_sections(self, store):
        engine.store_entry("Title", "Content")
        text = engine.get("K7E-000-00001")
        assert "## Verified Protocol" in text
        assert "## Edge Cases" in text
        assert "## False Paths" in text
        assert "## History" in text


class TestAppend:
    def test_append_adds_content(self, store):
        engine.store_entry("Title", "Original")
        engine.append_entry("K7E-000-00001", "Edge Cases", "New edge case")
        text = engine.get("K7E-000-00001")
        assert "New edge case" in text

    def test_append_bumps_verification(self, store):
        engine.store_entry("Title", "Original")
        engine.append_entry("K7E-000-00001", "Edge Cases", "Info")
        text = engine.get("K7E-000-00001")
        assert "verification_count: 1" in text

    def test_append_missing_raises(self, store):
        with pytest.raises(FileNotFoundError):
            engine.append_entry("K7E-999-99999", "Section", "Content")

    def test_append_creates_section(self, store):
        engine.store_entry("Title", "Original")
        engine.append_entry("K7E-000-00001", "New Section", "New content")
        text = engine.get("K7E-000-00001")
        assert "## New Section" in text


class TestSearch:
    def test_finds_by_title(self, store):
        engine.store_entry("Chrome Remote Debugging", "Use port 9222")
        results = engine.search("Chrome Remote Debugging")
        assert len(results) >= 1
        assert results[0]["id"] == "K7E-000-00001"

    def test_finds_by_content(self, store):
        engine.store_entry("Title", "use remote-debugging-port 9222")
        results = engine.search("remote debugging port")
        assert len(results) >= 1

    def test_no_results_for_unrelated(self, store):
        engine.store_entry("Chrome Stuff", "browser content")
        results = engine.search("quantum physics")
        assert len(results) == 0

    def test_ranked_results(self, store):
        engine.store_entry("Chrome Debugging", "chrome debug tools")
        engine.store_entry("Firefox Debugging", "firefox debug tools")
        results = engine.search("chrome debugging")
        assert results[0]["title"] == "Chrome Debugging"

    def test_limit(self, store):
        for i in range(10):
            engine.store_entry(f"Node {i}", f"content {i}")
        results = engine.search("content", limit=3)
        assert len(results) <= 3


class TestReindex:
    def test_rebuilds_from_files(self, store):
        engine.store_entry("Alpha", "alpha content")
        engine.store_entry("Beta", "beta content")
        # Nuke the index
        engine.INDEX_DB.unlink()
        engine.init()
        engine.reindex()
        results = engine.search("alpha")
        assert len(results) >= 1

    def test_idempotent(self, store):
        engine.store_entry("Node", "content")
        engine.reindex()
        engine.reindex()
        results = engine.search("content")
        assert len(results) == 1


class TestAssets:
    def test_store_asset(self, store, tmp_path):
        src = tmp_path / "photo.png"
        src.write_bytes(b"fake png data")
        rel_path = engine.store_asset(str(src))
        assert rel_path.startswith("assets/")
        assert rel_path.endswith(".png")
        # Bucketed: assets/XX/hash.ext
        parts = rel_path.split("/")
        assert len(parts) == 3

    def test_dedup(self, store, tmp_path):
        src1 = tmp_path / "a.mp4"
        src2 = tmp_path / "b.mp4"
        src1.write_bytes(b"same content")
        src2.write_bytes(b"same content")
        path1 = engine.store_asset(str(src1))
        path2 = engine.store_asset(str(src2))
        assert path1 == path2

    def test_different_content(self, store, tmp_path):
        src1 = tmp_path / "a.wav"
        src2 = tmp_path / "b.wav"
        src1.write_bytes(b"audio one")
        src2.write_bytes(b"audio two")
        path1 = engine.store_asset(str(src1))
        path2 = engine.store_asset(str(src2))
        assert path1 != path2

    def test_missing_raises(self, store):
        with pytest.raises(FileNotFoundError):
            engine.store_asset("/nonexistent.png")


class TestMOCs:
    def test_created_on_store(self, store):
        engine.store_entry("Title", "content", tags=["mytag"])
        moc = engine.MOCS_DIR / "mytag.md"
        assert moc.exists()
        assert "K7E-000-00001" in moc.read_text()

    def test_rebuild_mocs(self, store):
        engine.store_entry("A", "a", tags=["topic"])
        engine.store_entry("B", "b", tags=["topic"])
        engine.rebuild_mocs()
        moc = engine.MOCS_DIR / "topic.md"
        content = moc.read_text()
        assert "K7E-000-00001" in content
        assert "K7E-000-00002" in content


class TestStats:
    def test_returns_counts(self, store):
        engine.store_entry("A", "a", tags=["x"])
        engine.store_entry("B", "b", tags=["y"])
        s = engine.stats()
        assert s["total_nodes"] == 2
        assert s["total_mocs"] == 2
        assert s["avg_confidence"] == 0.5


class TestBucketing:
    def test_files_in_bucket_dir(self, store):
        engine.store_entry("Title", "Content")
        bucket_dir = engine.NODES_DIR / "000"
        assert bucket_dir.is_dir()
        files = list(bucket_dir.glob("K7E-*.md"))
        assert len(files) == 1
