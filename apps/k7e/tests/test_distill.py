"""Distill offline contract.

Text distillation requires an LLM — there is no offline pattern-matching
fallback. With no llm_command configured (the conftest default) extraction
yields nothing. Real extraction behavior is covered in test_llm_distill.py (@llm)."""
import distill


class TestDistillRequiresLLM:
    def test_offline_extracts_nothing(self, store, tmp_path):
        journal = tmp_path / "j.md"
        journal.write_text(
            "TIL: kubectl port-forward requires the pod to be Running.\n\n"
            "The fix is: add --vfs-cache-max-size 10G to cap cache growth.\n\n"
            "Use this command:\n```\nssh -L 8080:localhost:3000 user@host\n```\n"
        )
        results = distill.distill([str(journal)])
        assert results == [], f"Offline distill should extract nothing, got: {results}"

    def test_offline_short_text_noop(self, store, tmp_path):
        journal = tmp_path / "j.md"
        journal.write_text("short note")
        results = distill.distill([str(journal)])
        assert results == []
