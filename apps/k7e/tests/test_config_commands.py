"""Tests for explicit LLM command configuration."""
import config


class TestResolveCommand:
    def test_fallback_used_when_purpose_unset(self, store, monkeypatch):
        monkeypatch.setenv("K7E_LLM_COMMAND", "my-llm")
        monkeypatch.delenv("K7E_SUMMARIZE_COMMAND", raising=False)
        assert config.resolve_command("summarize") == "my-llm"

    def test_purpose_override_wins(self, store, monkeypatch):
        monkeypatch.setenv("K7E_LLM_COMMAND", "my-llm")
        monkeypatch.setenv("K7E_RERANK_COMMAND", "my-reranker")
        assert config.resolve_command("rerank") == "my-reranker"
        assert config.resolve_command("distill") == "my-llm"

    def test_none_when_unconfigured(self, store, monkeypatch):
        monkeypatch.delenv("K7E_LLM_COMMAND", raising=False)
        monkeypatch.delenv("K7E_DISTILL_COMMAND", raising=False)
        assert config.resolve_command("distill") is None

    def test_command_source_labels_override(self, store, monkeypatch):
        monkeypatch.setenv("K7E_COMPILE_COMMAND", "compile-cli")
        cmd, source = config.command_source("compile")
        assert cmd == "compile-cli"
        assert source == "compile_command"
