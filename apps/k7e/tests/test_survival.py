"""Knowledge survival tests — the core invariant."""
import shutil
import engine

class TestSurvivalCycle:
    def test_knowledge_survives_total_index_loss(self, store):
        """THE invariant: markdown files ARE the knowledge. Everything else rebuilds."""
        engine.store_entry("SSH Tunneling", "ssh -L 8080:target:80 bastion", tags=["ssh"])
        engine.store_entry("Docker Volumes", "Use named volumes for persistence", tags=["docker"])
        # Nuke everything except nodes/
        engine.INDEX_DB.unlink(missing_ok=True)
        shutil.rmtree(engine.MOCS_DIR)
        # Rebuild
        engine.init()
        engine.reindex()
        engine.rebuild_mocs()
        # Knowledge survived
        results = engine.search("SSH tunnel")
        assert len(results) >= 1
        assert results[0]["title"] == "SSH Tunneling"
        results = engine.search("docker named volumes")
        assert len(results) >= 1

    def test_full_roundtrip_20_facts(self, store):
        """Store 20 facts, destroy index, rebuild, verify all findable."""
        facts = [(f"Fact {i}", f"Unique-detail-{i}-specific-{i*7}", [f"cat{i%3}"]) for i in range(20)]
        ids = [engine.store_entry(t, c, tags=tg) for t, c, tg in facts]
        engine.INDEX_DB.unlink()
        engine.init()
        engine.reindex()
        misses = 0
        for i, (_, content, _) in enumerate(facts):
            results = engine.search(f"Unique-detail-{i}-specific-{i*7}")
            if not any(r["id"] == ids[i] for r in results):
                misses += 1
        assert misses == 0, f"Lost {misses}/20 facts after reindex"

    def test_append_survives_reindex(self, store):
        nid = engine.store_entry("Git Rebase", "Use git rebase -i for squash", tags=["git"])
        engine.append_entry(nid, "Edge Cases", "Never rebase shared branches")
        engine.INDEX_DB.unlink()
        engine.init()
        engine.reindex()
        results = engine.search("rebase shared branches")
        assert any(r["id"] == nid for r in results)

    def test_append_content_searchable(self, store):
        nid = engine.store_entry("Base Entry", "Original content only", tags=["test"])
        engine.append_entry(nid, "Edge Cases", "Appended unique xylophone content")
        results = engine.search("xylophone")
        assert len(results) >= 1
        assert results[0]["id"] == nid
