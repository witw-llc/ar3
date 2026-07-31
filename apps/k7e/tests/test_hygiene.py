"""Tests for the hygiene auditor's index/store agreement check (#79)."""
import engine
import hygiene


class TestIndexDisagreement:
    def test_agrees_on_an_empty_store(self, store):
        assert hygiene.index_disagreement() is None

    def test_agrees_after_a_normal_store(self, store):
        engine.store_entry("Redis Port", "Redis default port is 6379", tags=["redis"])
        engine.store_entry("Redis AOF", "Redis supports AOF persistence", tags=["redis"])
        assert hygiene.index_disagreement() is None

    def test_flags_a_deleted_index(self, store):
        engine.store_entry("Redis Port", "Redis default port is 6379", tags=["redis"])
        engine.store_entry("Redis AOF", "Redis supports AOF persistence", tags=["redis"])
        engine.INDEX_DB.unlink()

        message = hygiene.index_disagreement()
        assert message == "2 entr(ies), 0 indexed — run k7e reindex"

    def test_flags_a_stale_index_after_hand_deleting_a_node_file(self, store):
        """The index can also be ahead of the store: a node file removed
        outside k7e (hand-deleted, not via a k7e command) leaves the index
        counting a node the store no longer has."""
        engine.store_entry("Redis Port", "Redis default port is 6379", tags=["redis"])
        engine.store_entry("Redis AOF", "Redis supports AOF persistence", tags=["redis"])
        next(engine._all_node_files()).unlink()

        message = hygiene.index_disagreement()
        assert message == "1 entr(ies), 2 indexed — run k7e reindex"

    def test_reindex_resolves_the_disagreement(self, store):
        engine.store_entry("Redis Port", "Redis default port is 6379", tags=["redis"])
        engine.INDEX_DB.unlink()
        assert hygiene.index_disagreement() is not None

        engine.reindex()
        assert hygiene.index_disagreement() is None


class TestCheckCLIReportsDisagreement:
    def test_clean_store_prints_clean_with_no_disagreement_line(self, store, capsys):
        import cli

        exit_code = cli.main(["check"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "Markdown clean." in out
        assert "indexed" not in out

    def test_deleted_index_prints_disagreement_alongside_clean_markdown(self, store, capsys):
        import cli

        engine.store_entry("Redis Port", "Redis default port is 6379", tags=["redis"])
        engine.INDEX_DB.unlink()

        exit_code = cli.main(["check"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "Markdown clean." in out
        assert "Index: 1 entr(ies), 0 indexed — run k7e reindex" in out

    def test_markdown_issues_and_disagreement_both_reported_and_scoped(self, store, capsys):
        """When the markdown audit finds issues *and* the index disagrees,
        both must be visible and unambiguous: the issue count covers only
        markdown issues, and the index line is clearly its own scope."""
        import cli

        engine.store_entry("Redis Port", "Redis default port is 6379", tags=[])
        engine.INDEX_DB.unlink()

        exit_code = cli.main(["check"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "1 issue(s)." in out
        assert "Index: 1 entr(ies), 0 indexed — run k7e reindex" in out
        assert "Markdown clean." not in out
