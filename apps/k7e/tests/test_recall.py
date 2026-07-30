"""Tests for k7e recall (RAG) and _call_llm helper."""
import engine


class TestRecallNoLLM:
    """Recall behavior when no LLM is available (most test environments)."""

    def test_empty_store_returns_nothing(self, store):
        answer, sources = engine.recall("anything at all")
        assert answer is None
        assert sources == []

    def test_empty_input_returns_nothing(self, store):
        answer, sources = engine.recall("")
        assert answer is None
        assert sources == []

    def test_whitespace_input_returns_nothing(self, store):
        answer, sources = engine.recall("   \n\t  ")
        assert answer is None
        assert sources == []

    def test_returns_sources_without_answer(self, store):
        """When nodes match but no LLM is available, sources are still returned."""
        engine.store_entry("Redis Port", "Redis default port is 6379", tags=["redis"])
        engine.store_entry("Redis Persistence", "Redis supports RDB and AOF", tags=["redis"])
        answer, sources = engine.recall("redis port")
        # No LLM → no synthesis, but sources should be found
        assert answer is None
        assert len(sources) >= 1
        assert any("Redis" in s["title"] for s in sources)

    def test_unrelated_query_finds_nothing(self, store):
        engine.store_entry("Redis Port", "Redis default port is 6379", tags=["redis"])
        answer, sources = engine.recall("quantum chromodynamics gluon plasma")
        assert answer is None
        assert sources == []

    def test_limit_respected(self, store):
        for i in range(10):
            engine.store_entry(f"Fact {i}", f"Knowledge about topic number {i}", tags=["facts"])
        _, sources = engine.recall("knowledge topic", limit=3)
        assert len(sources) <= 3

    def test_long_input_searches_raw_text(self, store):
        """Long input without LLM: decompose returns nothing, so recall searches
        the raw text directly. Key assertion: no crash, sources is a list."""
        engine.store_entry("SSH Tunneling", "Use ssh -L for local port forwarding", tags=["ssh"])
        long_text = (
            "We were discussing SSH tunneling and port forwarding techniques "
            "for securely accessing services behind firewalls. The conversation "
            "covered both local and remote forwarding patterns."
        )
        _, sources = engine.recall(long_text)
        assert isinstance(sources, list)


class TestCallLlm:
    """Test _call_llm graceful failures."""

    def test_returns_none_when_no_command(self, store, monkeypatch):
        monkeypatch.delenv("K7E_LLM_COMMAND", raising=False)
        monkeypatch.delenv("K7E_SUMMARIZE_COMMAND", raising=False)
        result = engine._call_llm("test prompt", purpose="summarize")
        assert result is None

    def test_returns_none_on_timeout(self, store, monkeypatch, tmp_path):
        script = tmp_path / "slow.sh"
        script.write_text("#!/usr/bin/env bash\nsleep 5\n")
        script.chmod(0o755)
        monkeypatch.setenv("K7E_LLM_COMMAND", str(script))
        result = engine._call_llm("test", purpose="summarize", timeout=1)
        assert result is None


class TestDecomposeQueries:
    """Without an LLM, _decompose_queries returns [] (no word-split fallback);
    recall() then searches the raw text."""

    def test_returns_empty_without_llm(self, store):
        queries = engine._decompose_queries("short text only four words here extra padding needed")
        assert queries == []

    def test_empty_returns_empty(self, store):
        queries = engine._decompose_queries("")
        assert queries == []


class TestRecallCLI:
    """CLI recall fails fast when no LLM command is configured."""

    def test_recall_fails_fast_without_llm(self, store, monkeypatch, capsys):
        import io
        import cli

        monkeypatch.setattr("sys.stdin", io.StringIO("redis port forwarding"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        exit_code = cli.main(["recall"])
        assert exit_code == 1
        assert "requires an LLM" in capsys.readouterr().err

    def test_recall_with_text_arg_fails_fast(self, store, capsys):
        import cli
        engine.store_entry("Test Node", "Some test content for recall", tags=["test"])
        exit_code = cli.main(["recall", "test content"])
        assert exit_code == 1
        assert "requires an LLM" in capsys.readouterr().err
