"""Distill offline contract.

Text distillation requires an LLM — there is no offline pattern-matching
fallback. With no llm_command configured (the conftest default) extraction
yields nothing. Real extraction behavior is covered in test_llm_distill.py (@llm).

Stub-LLM cases exercise response-shape handling without a live model."""
import json

import distill
import engine


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


class TestDistillContentType:
    """Non-string LLM content must skip the bad candidate, not abort the batch."""

    def test_list_typed_content_skips_candidate(self, store, tmp_path, monkeypatch, capsys):
        payload = [
            {
                "title": "Redis default port",
                "content": (
                    "Redis listens on TCP port 6379 by default and stores "
                    "that binding in redis.conf under the port directive."
                ),
                "tags": ["redis"],
            },
            {
                "title": "Malformed list content",
                "content": ["first fragment", "second fragment"],
                "tags": ["bad"],
            },
            {
                "title": "PostgreSQL default port",
                "content": (
                    "PostgreSQL accepts TCP connections on port 5432 by default "
                    "unless listen_addresses and port are overridden."
                ),
                "tags": ["postgres"],
            },
        ]
        wrapper = tmp_path / "fake-llm.py"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "sys.stdin.read()\n"
            f"print({json.dumps(payload)!r})\n"
        )
        wrapper.chmod(0o755)
        monkeypatch.setenv("K7E_LLM_COMMAND", str(wrapper))

        source = tmp_path / "notes.md"
        source.write_text(
            "Notes from ops review. Redis listens on 6379. PostgreSQL listens "
            "on 5432. Document both so the next on-call shift has the ports "
            "without guessing. Extra padding so the distill length gate opens.\n"
        )

        import cli

        exit_code = cli.main(["distill", str(source)])
        assert exit_code == 0

        err = capsys.readouterr().err
        assert "content is list" in err
        assert "Malformed list content" in err

        titles = {n["title"] for n in engine.list_nodes(status="active")}
        assert "Redis default port" in titles
        assert "PostgreSQL default port" in titles
        assert "Malformed list content" not in titles
