"""Member-level knowledge (#41): the `Knowledge:` roster flag, inject on wake,
dream (batch distill) on idle. Default off must be byte-invisible."""
from __future__ import annotations

import subprocess

import pytest

import dispatch
import knowledge
import state
from rig import Rig
from roster import load_roster, parse_knowledge

NODE = "acme"


def read_log():
    files = (state.roster_dir(NODE) / "log").glob("*.md")
    return "".join(f.read_text(encoding="utf-8") for f in files)


def seed_store(name, title, content):
    home = knowledge.store_home(NODE, name)
    res = knowledge._run_k7e(home, "store", title, "--content", content)
    assert res.returncode == 0, res.stderr


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class TestParseKnowledge:
    def test_values(self):
        assert parse_knowledge("off") == 0
        assert parse_knowledge("") == 0
        assert parse_knowledge("on") == 2048
        assert parse_knowledge("4k") == 4096
        assert parse_knowledge("4096") == 4096

    def test_garbage_is_an_error(self):
        with pytest.raises(ValueError):
            parse_knowledge("lots")

    def test_roster_field_parses_and_bad_value_surfaces(self, tmp_path):
        path = tmp_path / "ROSTER.md"
        path.write_text(
            "# R\n\n### Wren\n- **Rig:** claude\n- **Knowledge:** 4k\n\n"
            "### Bad\n- **Rig:** claude\n- **Knowledge:** lots\n",
            encoding="utf-8",
        )
        roster = load_roster(path)
        assert roster.find("wren").knowledge_bytes == 4096
        assert "Knowledge must be" in roster.find("bad").error

    def test_absent_field_is_off(self, ctx):
        roster = load_roster(ctx.roster_path)
        assert all(m.knowledge_bytes == 0 for m in roster.members)


class TestKnowledgeSection:
    def test_off_is_byte_invisible(self, ctx):
        roster = load_roster(ctx.roster_path)
        sections = dispatch.prompt_sections(
            ctx, roster, roster.find("phil"), [], Rig(name="t")
        )
        assert "knowledge" not in [label for label, _ in sections]

    def test_on_with_no_store_is_silent(self, ctx):
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_bytes = 2048
        assert knowledge.knowledge_section(ctx, phil, []) == []
        assert "KNOWLEDGE" not in read_log()

    def test_store_hit_lands_before_reinforce_with_provenance(self, ctx):
        seed_store("phil", "Deploy key limits", "The deploy key cannot push workflow files.")
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_bytes = 2048
        phil.reinforce = "stay in your lane"
        sections = dispatch.prompt_sections(
            ctx, roster, phil, [{"body": "deploy key problem"}], Rig(name="t")
        )
        labels = [label for label, _ in sections]
        assert labels[-2:] == ["knowledge", "reinforce"]
        text = "\n".join(p for _l, parts in sections for p in parts)
        assert knowledge.KNOWLEDGE_HEADER in text
        assert knowledge.KNOWLEDGE_FRAMING in text
        assert "(K7E-" in text  # provenance id on the snippet
        assert "cannot push workflow files" in text

    def test_budget_truncates_deterministically(self, ctx):
        seed_store("phil", "Long note", "word " * 500)
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_bytes = 120
        batch = [{"body": "that long note about words"}]
        one = knowledge.knowledge_section(ctx, phil, batch)
        two = knowledge.knowledge_section(ctx, phil, batch)
        assert one == two
        blocks = one[3:]  # after header, framing, blank
        assert blocks
        assert sum(len(b.encode("utf-8")) for b in blocks if b) <= 120

    def test_echo_member_never_gets_the_section(self, ctx):
        seed_store("phil", "Note", "content")
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_bytes = 2048
        sections = dispatch.prompt_sections(
            ctx, roster, phil, [], Rig(name="t", echo=True)
        )
        assert "knowledge" not in [label for label, _ in sections]

    def test_search_failure_skips_and_logs(self, ctx, monkeypatch):
        home = knowledge.store_home(NODE, "phil")
        (home / "nodes").mkdir(parents=True)
        monkeypatch.setattr(
            knowledge, "_run_k7e",
            lambda *a, **k: completed(returncode=1, stderr="index exploded"),
        )
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_bytes = 2048
        assert knowledge.knowledge_section(ctx, phil, []) == []
        assert "KNOWLEDGE-SKIP phil" in read_log()

    def test_prompt_stats_price_the_section(self, ctx):
        seed_store("phil", "Deploy key limits", "The deploy key cannot push workflow files.")
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_bytes = 2048
        sections = dispatch.prompt_sections(
            ctx, roster, phil, [{"body": "the deploy key again"}], Rig(name="t")
        )
        stats = dict(dispatch.prompt_stats(sections))
        assert stats["knowledge"] > 0


class TestDreamSweep:
    def dreaming_roster(self, ctx, budget=2048):
        roster = load_roster(ctx.roster_path)
        roster.find("phil").knowledge_bytes = budget
        return roster

    def test_off_members_are_never_distilled(self, ctx, monkeypatch):
        calls = []
        monkeypatch.setattr(
            knowledge, "_run_k7e",
            lambda home, *a, **k: calls.append(a) or completed(),
        )
        state.write_turn_capture(NODE, "gerry", "20260730T000000000000Z", "t", "x")
        roster = load_roster(ctx.roster_path)
        assert knowledge.dream_sweep(ctx, roster) == []
        assert calls == []

    def test_fresh_captures_distill_and_watermark_advances(self, ctx, monkeypatch):
        calls = []
        monkeypatch.setattr(
            knowledge, "_run_k7e",
            lambda home, *a, **k: calls.append(a) or completed(stdout="ok"),
        )
        state.write_turn_capture(NODE, "phil", "20260730T000000000001Z", "t", "one")
        state.write_turn_capture(NODE, "phil", "20260730T000000000002Z", "t", "two")
        roster = self.dreaming_roster(ctx)
        assert knowledge.dream_sweep(ctx, roster) == ["Phil"]
        (call,) = calls
        assert call[0] == "distill" and len(call) == 3
        mark = knowledge.store_home(NODE, "phil") / ".dreamed"
        assert mark.read_text(encoding="utf-8").strip().startswith(
            "20260730T000000000002Z"
        )
        assert "DREAM phil distilled 2" in read_log()
        # Nothing new: the next sweep is a no-op.
        calls.clear()
        assert knowledge.dream_sweep(ctx, roster) == []
        assert calls == []

    def test_failure_leaves_the_watermark(self, ctx, monkeypatch):
        monkeypatch.setattr(
            knowledge, "_run_k7e",
            lambda *a, **k: completed(returncode=1, stderr="k7e distill requires an LLM command."),
        )
        state.write_turn_capture(NODE, "phil", "20260730T000000000003Z", "t", "x")
        roster = self.dreaming_roster(ctx)
        assert knowledge.dream_sweep(ctx, roster) == []
        assert not (knowledge.store_home(NODE, "phil") / ".dreamed").exists()
        assert "DREAM-SKIP phil" in read_log()

    def test_batch_is_bounded_per_pass(self, ctx, monkeypatch):
        calls = []
        monkeypatch.setattr(
            knowledge, "_run_k7e",
            lambda home, *a, **k: calls.append(a) or completed(),
        )
        for i in range(knowledge.DREAM_BATCH + 3):
            state.write_turn_capture(NODE, "phil", f"20260730T00000000001{i}Z", "t", "x")
        roster = self.dreaming_roster(ctx)
        knowledge.dream_sweep(ctx, roster)
        (call,) = calls
        assert len(call) - 1 == knowledge.DREAM_BATCH  # "distill" + bounded paths
        calls.clear()
        knowledge.dream_sweep(ctx, roster)  # the remainder rides the next pass
        (call,) = calls
        assert len(call) - 1 == 3

    def test_run_idle_reports_dreaming(self, ctx):
        result = dispatch.run_idle(ctx)
        assert result["dreamed"] == []
