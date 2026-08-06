"""Member-level knowledge (#41/#52): the `Knowledge:` roster flag, inject on
wake, dream (batch distill) on idle. Default off must be byte-invisible."""
from __future__ import annotations

import datetime
import re
import sqlite3
import subprocess

import pytest

import dispatch
import knowledge
import state
from rig import Rig, load_rig_config
from roster import (
    KNOWLEDGE_DEFAULT_BUDGET,
    KNOWLEDGE_SIZES,
    FramingSpec,
    KnowledgeSpec,
    load_roster,
    parse_framing,
    parse_knowledge,
)

NODE = "acme"


def read_log():
    files = (state.roster_dir(NODE) / "log").glob("*.md")
    return "".join(f.read_text(encoding="utf-8") for f in files)


def seed_store(name, title, content):
    home = knowledge.store_home(NODE, name)
    res = knowledge._run_k7e(home, "store", title, "--content", content)
    assert res.returncode == 0, res.stderr


def _node_path(home, node_id):
    return next((home / "nodes").glob(f"**/{node_id}.md"))


def use_count(home, node_id):
    conn = sqlite3.connect(home / ".index.db")
    row = conn.execute("SELECT use_count FROM nodes WHERE id = ?", (node_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def seed_dated_store(name, title, content, days_old):
    """A store entry backdated `days_old` days by rewriting k7e's own
    `last_updated:` frontmatter field, then reindexing so the FTS/recency
    index agrees with the file — the age stamp reads the file, but search
    ranking reads the index."""
    home = knowledge.store_home(NODE, name)
    res = knowledge._run_k7e(home, "store", title, "--content", content)
    assert res.returncode == 0, res.stderr
    node_id = res.stdout.split()[1].rstrip(":")
    path = _node_path(home, node_id)
    stamp = (datetime.date.today() - datetime.timedelta(days=days_old)).isoformat()
    text = re.sub(r"last_updated: .+", f"last_updated: {stamp}", path.read_text(encoding="utf-8"))
    path.write_text(text, encoding="utf-8")
    reindexed = knowledge._run_k7e(home, "reindex")
    assert reindexed.returncode == 0, reindexed.stderr
    return node_id


def seed_undated_store(name, title, content):
    """A store entry with no `last_updated:` line at all — the no-parseable-
    date case `_entry_snippet` already handles with the bare-id form."""
    home = knowledge.store_home(NODE, name)
    res = knowledge._run_k7e(home, "store", title, "--content", content)
    assert res.returncode == 0, res.stderr
    node_id = res.stdout.split()[1].rstrip(":")
    path = _node_path(home, node_id)
    text = re.sub(r"\nlast_updated: .+\n", "\n", path.read_text(encoding="utf-8"))
    path.write_text(text, encoding="utf-8")
    reindexed = knowledge._run_k7e(home, "reindex")
    assert reindexed.returncode == 0, reindexed.stderr
    return node_id


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class TestParseKnowledge:
    def test_off_forms(self):
        for v in ("off", "", "no", "false"):
            assert parse_knowledge(v) is None

    def test_bare_on_forms(self):
        for v in ("on", "yes", "true"):
            assert parse_knowledge(v) == KnowledgeSpec()

    def test_tshirt_sizes(self):
        assert parse_knowledge("small") == KnowledgeSpec(size_bytes=4096)
        assert parse_knowledge("medium") == KnowledgeSpec(size_bytes=8192)
        assert parse_knowledge("large") == KnowledgeSpec(size_bytes=32768)
        assert parse_knowledge("LARGE") == KnowledgeSpec(size_bytes=32768)
        assert KNOWLEDGE_SIZES == {"small": 4096, "medium": 8192, "large": 32768}
        assert KNOWLEDGE_DEFAULT_BUDGET == KNOWLEDGE_SIZES["small"]

    def test_exact_byte_escape_hatch(self):
        assert parse_knowledge("4k") == KnowledgeSpec(size_bytes=4096)
        assert parse_knowledge("4096") == KnowledgeSpec(size_bytes=4096)
        assert parse_knowledge("4kb") == KnowledgeSpec(size_bytes=4096)

    def test_bare_rig_name_is_a_distill_override_at_default_budget(self):
        assert parse_knowledge("claude") == KnowledgeSpec(distill_rig="claude")
        assert parse_knowledge("Claude") == KnowledgeSpec(distill_rig="claude")

    def test_size_and_rig_combine_in_either_order(self):
        assert parse_knowledge("4k claude") == KnowledgeSpec(
            size_bytes=4096, distill_rig="claude"
        )
        assert parse_knowledge("large agy") == KnowledgeSpec(
            size_bytes=32768, distill_rig="agy"
        )
        assert parse_knowledge("agy large") == KnowledgeSpec(
            size_bytes=32768, distill_rig="agy"
        )

    @pytest.mark.parametrize("bad", [
        "lots of context",       # more than two tokens
        "small medium",          # two sizes
        "claude codex",          # two rigs
        "lots!",                 # not a usable rig-name shape
        "4kb!",                  # not a usable size or rig-name shape
    ])
    def test_garbage_is_an_error(self, bad):
        with pytest.raises(ValueError):
            parse_knowledge(bad)

    def test_roster_field_parses_and_bad_value_surfaces(self, tmp_path):
        path = tmp_path / "ROSTER.md"
        path.write_text(
            "# R\n\n"
            "### Wren\n- **Rig:** claude\n- **Knowledge:** 4k\n\n"
            "### Robin\n- **Rig:** claude\n- **Knowledge:** large agy\n\n"
            "### Bad\n- **Rig:** claude\n- **Knowledge:** small medium\n",
            encoding="utf-8",
        )
        roster = load_roster(path)
        wren = roster.find("wren")
        assert (wren.knowledge_on, wren.knowledge_bytes, wren.knowledge_distill_rig) == (
            True, 4096, None,
        )
        robin = roster.find("robin")
        assert (robin.knowledge_on, robin.knowledge_bytes, robin.knowledge_distill_rig) == (
            True, 32768, "agy",
        )
        assert "Knowledge names two sizes" in roster.find("bad").error

    def test_absent_field_is_off(self, ctx):
        roster = load_roster(ctx.roster_path)
        assert all(
            (m.knowledge_on, m.knowledge_bytes, m.knowledge_distill_rig)
            == (False, None, None)
            for m in roster.members
        )


class TestParseFraming:
    """`Framing:` (#62): `default`/absent keeps the built-in line, `off`
    drops it, a double-quoted string is custom wording. Quotes are mandatory
    for a roster line — without them "off"/"default" can't be told apart
    from an operator's own sentence that starts with those words."""

    def test_default_forms(self):
        for v in ("default", "", "DEFAULT", "Default"):
            assert parse_framing(v) == FramingSpec()

    def test_off_forms(self):
        for v in ("off", "OFF", "Off"):
            assert parse_framing(v) == FramingSpec(off=True)

    def test_quoted_custom_text(self):
        assert parse_framing('"background, use with care"') == FramingSpec(
            text="background, use with care"
        )

    def test_unquoted_custom_text_is_an_error(self):
        with pytest.raises(ValueError, match="double-quoted"):
            parse_framing("background, use with care")

    def test_unquoted_off_lookalike_sentence_is_an_error(self):
        with pytest.raises(ValueError, match="double-quoted"):
            parse_framing("off the record notes")

    def test_rig_config_form_does_not_require_quotes(self):
        assert parse_framing("some custom framing", quoted=False) == FramingSpec(
            text="some custom framing"
        )
        assert parse_framing("off", quoted=False) == FramingSpec(off=True)
        assert parse_framing("default", quoted=False) == FramingSpec()

    def test_roster_field_parses_and_bad_value_surfaces(self, tmp_path):
        path = tmp_path / "ROSTER.md"
        path.write_text(
            "# R\n\n"
            "### Wren\n- **Rig:** claude\n- **Framing:** off\n\n"
            '### Robin\n- **Rig:** claude\n- **Framing:** "watch for stale notes"\n\n'
            "### Bad\n- **Rig:** claude\n- **Framing:** unquoted prose\n",
            encoding="utf-8",
        )
        roster = load_roster(path)
        assert roster.find("wren").framing == FramingSpec(off=True)
        assert roster.find("robin").framing == FramingSpec(text="watch for stale notes")
        assert "double-quoted" in roster.find("bad").error

    def test_absent_field_is_none(self, ctx):
        roster = load_roster(ctx.roster_path)
        assert all(m.framing is None for m in roster.members)


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
        phil.knowledge_on = True
        assert knowledge.knowledge_section(ctx, phil, []) == []
        assert "KNOWLEDGE" not in read_log()

    def test_store_hit_lands_before_reinforce_with_provenance(self, ctx):
        seed_store("phil", "Deploy key limits", "The deploy key cannot push workflow files.")
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
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
        phil.knowledge_on = True
        phil.knowledge_bytes = 400  # room for the preamble + a truncated snippet
        batch = [{"body": "that long note about words"}]
        one = knowledge.knowledge_section(ctx, phil, batch)
        two = knowledge.knowledge_section(ctx, phil, batch)
        assert one == two
        blocks = one[3:]  # after header, framing, blank
        assert blocks
        assert sum(len(b.encode("utf-8")) for b in blocks if b) <= 400

    def test_starved_budget_skips_the_entry_instead_of_a_stub(self, ctx):
        """A budget too small for even one preamble + MIN_SNIPPET bytes of
        snippet packs nothing — the rank-proportional packer never emits a
        provenance-only stub (k-budget-packing, #12/#52)."""
        seed_store("phil", "Long note", "word " * 500)
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        phil.knowledge_bytes = 40  # below any preamble + knowledge.MIN_SNIPPET
        batch = [{"body": "that long note about words"}]
        assert knowledge.knowledge_section(ctx, phil, batch) == []
        assert "KNOWLEDGE phil 0 entries 0B" in read_log()

    def test_small_budget_covers_more_entries_than_the_old_greedy_packer(self, ctx):
        """The rank-proportional packer spreads a small budget across several
        ranked entries instead of spending it whole on rank 1 — the old
        greedy-whole loop covered at most one entry at this budget."""
        for i in range(4):
            seed_store("phil", f"Note {i}", f"content {i} " + "word " * 300)
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        phil.knowledge_bytes = KNOWLEDGE_DEFAULT_BUDGET
        batch = [{"body": "content note word"}]
        parts = knowledge.knowledge_section(ctx, phil, batch)
        entry_count = sum(1 for p in parts if p.startswith("### "))
        assert entry_count > 1
        used = sum(len(p.encode("utf-8")) for p in parts[3:] if p)
        assert used <= KNOWLEDGE_DEFAULT_BUDGET

    def test_echo_member_never_gets_the_section(self, ctx):
        seed_store("phil", "Note", "content")
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
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
        phil.knowledge_on = True
        assert knowledge.knowledge_section(ctx, phil, []) == []
        assert "KNOWLEDGE-SKIP phil" in read_log()

    def test_wake_prices_the_search_and_falls_back_to_fts(self, ctx):
        """No ollama at the other end: the section still lands, and the day log
        says what the unanswered semantic track cost the wake."""
        seed_store("phil", "Deploy key limits", "The deploy key cannot push workflow files.")
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        assert knowledge.knowledge_section(ctx, phil, [{"body": "deploy key problem"}])
        log = read_log()
        assert "KNOWLEDGE phil 1 entry" in log
        assert "unanswered, fts-only)" in log

    def test_embed_note_reads_k7e_timing(self):
        assert knowledge._embed_note("embed 41ms\n") == "embed 41ms"
        assert knowledge._embed_note("") == "fts-only"
        assert (
            knowledge._embed_note("embed 2011ms (semantic track unavailable)\n")
            == "embed 2011ms unanswered, fts-only"
        )

    def test_prompt_stats_price_the_section(self, ctx):
        seed_store("phil", "Deploy key limits", "The deploy key cannot push workflow files.")
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        sections = dispatch.prompt_sections(
            ctx, roster, phil, [{"body": "the deploy key again"}], Rig(name="t")
        )
        stats = dict(dispatch.prompt_stats(sections))
        assert stats["knowledge"] > 0

    def test_no_explicit_size_ties_the_budget_to_the_turn_rig_tier(self, ctx):
        seed_store("phil", "Deploy notes", "word " * 1200)  # bigger than `small`
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        batch = [{"body": "deploy notes"}]
        no_preset = knowledge.knowledge_section(ctx, phil, batch, Rig(name="t"))
        big_tier = knowledge.knowledge_section(
            ctx, phil, batch, Rig(name="t", preset="claude")
        )
        assert no_preset and big_tier
        no_preset_bytes = sum(len(p.encode("utf-8")) for p in no_preset[3:] if p)
        big_tier_bytes = sum(len(p.encode("utf-8")) for p in big_tier[3:] if p)
        assert no_preset_bytes <= KNOWLEDGE_DEFAULT_BUDGET
        assert big_tier_bytes <= KNOWLEDGE_SIZES["large"]
        assert big_tier_bytes > no_preset_bytes


class TestUsageTracking:
    """Injection, not fetch, counts as a recall (#12/#52): sizing reads
    every pool entry with `get --no-track`, and a single `touch` afterward
    bumps k7e's usage ranking signal only for entries the packer kept."""

    def test_no_track_get_does_not_bump_use_count(self, ctx):
        home = knowledge.store_home(NODE, "phil")
        res = knowledge._run_k7e(
            home, "store", "Deploy key limits",
            "--content", "The deploy key cannot push workflow files.",
        )
        assert res.returncode == 0, res.stderr
        node_id = res.stdout.split()[1].rstrip(":")
        got = knowledge._run_k7e(home, "get", node_id, "--no-track")
        assert got.returncode == 0, got.stderr
        assert use_count(home, node_id) == 0

    def test_touch_bumps_use_count_without_reading(self, ctx):
        home = knowledge.store_home(NODE, "phil")
        res = knowledge._run_k7e(
            home, "store", "Deploy key limits",
            "--content", "The deploy key cannot push workflow files.",
        )
        assert res.returncode == 0, res.stderr
        node_id = res.stdout.split()[1].rstrip(":")
        touched = knowledge._run_k7e(home, "touch", node_id)
        assert touched.returncode == 0, touched.stderr
        assert use_count(home, node_id) == 1

    def test_sizing_fetch_is_untracked_only_injected_entries_are_touched(self, ctx):
        """A budget that starves the low-ranked entries in the weighting pool
        (#12/#52) still fetches all of them to size the split, but only the
        ones the packer actually injects gain use_count — a fetch spent on an
        entry the model never sees is not a recall."""
        home = knowledge.store_home(NODE, "phil")
        ids = []
        for i in range(knowledge.RANK_POOL):
            res = knowledge._run_k7e(
                home, "store", f"Note {i}",
                "--content", f"content note {i} " + "word " * 100,
            )
            assert res.returncode == 0, res.stderr
            ids.append(res.stdout.split()[1].rstrip(":"))
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        phil.knowledge_bytes = 1024  # starves the low-weight tail of the pool
        batch = [{"body": "content note word"}]
        parts = knowledge.knowledge_section(ctx, phil, batch)
        text = "\n".join(parts)
        injected = {m for m in ids if f"({m}" in text}
        skipped = set(ids) - injected
        assert injected and skipped  # the budget must actually starve someone
        for node_id in injected:
            assert use_count(home, node_id) == 1
        for node_id in skipped:
            assert use_count(home, node_id) == 0


class TestFramingInSection:
    """The rendered `## Knowledge` section under Framing (#62): `off` removes
    only the framing line — header, provenance, and entries are untouched —
    and the section stays byte-identical to today when nothing is set."""

    def _seed_and_batch(self):
        seed_store("phil", "Deploy key limits", "The deploy key cannot push workflow files.")
        return [{"body": "deploy key problem"}]

    def test_default_is_byte_identical_to_today(self, ctx):
        batch = self._seed_and_batch()
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        parts = knowledge.knowledge_section(ctx, phil, batch)
        assert parts[0] == knowledge.KNOWLEDGE_HEADER
        assert parts[1] == knowledge.KNOWLEDGE_FRAMING
        assert parts[2] == ""

    def test_member_off_drops_only_the_framing_line(self, ctx):
        batch = self._seed_and_batch()
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        phil.framing = FramingSpec(off=True)
        parts = knowledge.knowledge_section(ctx, phil, batch)
        assert parts[0] == knowledge.KNOWLEDGE_HEADER
        assert parts[1] == ""
        assert knowledge.KNOWLEDGE_FRAMING not in parts
        assert "(K7E-" in "\n".join(parts)
        assert any("cannot push workflow files" in p for p in parts)

    def test_member_custom_text_replaces_the_built_in_line(self, ctx):
        batch = self._seed_and_batch()
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        phil.framing = FramingSpec(text="custom framing wording")
        parts = knowledge.knowledge_section(ctx, phil, batch)
        assert parts[1] == "custom framing wording"
        assert knowledge.KNOWLEDGE_FRAMING not in parts

    def test_member_explicit_choice_wins_over_rig_default(self, ctx):
        batch = self._seed_and_batch()
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        phil.framing = FramingSpec(text="member wins")
        rig = Rig(name="t", framing=FramingSpec(off=True))
        parts = knowledge.knowledge_section(ctx, phil, batch, rig)
        assert parts[1] == "member wins"

    def test_rig_default_applies_when_member_is_silent(self, ctx):
        batch = self._seed_and_batch()
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        rig = Rig(name="t", framing=FramingSpec(off=True))
        parts = knowledge.knowledge_section(ctx, phil, batch, rig)
        assert parts[0] == knowledge.KNOWLEDGE_HEADER
        assert parts[1] == ""
        assert knowledge.KNOWLEDGE_FRAMING not in parts


class TestAgeStamp:
    """Relative-age provenance + staleness status line (#62, k-age-presentation):
    the measured production change replacing the absolute-date stamp."""

    def test_fresh_entry_stamps_today_with_no_status_line(self, ctx):
        node_id_res = knowledge._run_k7e(
            knowledge.store_home(NODE, "phil"), "store", "Deploy key limits",
            "--content", "The deploy key cannot push workflow files.",
        )
        assert node_id_res.returncode == 0, node_id_res.stderr
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        text = "\n".join(
            knowledge.knowledge_section(ctx, phil, [{"body": "deploy key problem"}])
        )
        assert ", today)" in text
        assert knowledge.KNOWLEDGE_STATUS_LINE not in text

    def test_stale_entry_stamps_days_old_with_status_line(self, ctx):
        node_id = seed_dated_store(
            "phil", "Old deploy runbook", "Deploys go out weekly on Fridays from main.", 36,
        )
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        text = "\n".join(
            knowledge.knowledge_section(ctx, phil, [{"body": "deploy runbook schedule"}])
        )
        assert f"({node_id}, 36d old)" in text
        assert knowledge.KNOWLEDGE_STATUS_LINE in text
        assert text.index(knowledge.KNOWLEDGE_STATUS_LINE) < text.index(
            "Deploys go out weekly"
        )

    @pytest.mark.parametrize("days_old,expect_line", [(29, False), (31, True)])
    def test_staleness_threshold_boundary(self, ctx, days_old, expect_line):
        seed_dated_store("phil", "Boundary note", "Some boundary content here.", days_old)
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        text = "\n".join(
            knowledge.knowledge_section(ctx, phil, [{"body": "boundary note content"}])
        )
        assert f"{days_old}d old)" in text
        assert (knowledge.KNOWLEDGE_STATUS_LINE in text) is expect_line

    def test_undated_entry_keeps_bare_id_stamp(self, ctx):
        node_id = seed_undated_store("phil", "Undated note", "Content with no date at all.")
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        text = "\n".join(
            knowledge.knowledge_section(ctx, phil, [{"body": "undated note content"}])
        )
        assert f"({node_id})" in text
        assert f"{node_id}," not in text
        assert knowledge.KNOWLEDGE_STATUS_LINE not in text

    def test_status_line_bytes_count_against_the_budget(self, ctx):
        """Same title/content on two members, one just under the threshold
        (no status line) and one just over (status line) — same-length
        relative-age label (`20d old` / `40d old`) isolates the status line
        as the only source of the byte-size difference, proving it rides the
        same `size = len(block.encode(...))` accounting as everything else."""
        body = "word " * 40
        seed_dated_store("phil", "Byte budget note", body, 20)
        seed_dated_store("gerry", "Byte budget note", body, 40)
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        gerry = roster.find("gerry")
        gerry.knowledge_on = True
        batch = [{"body": "byte budget note"}]
        fresh_parts = knowledge.knowledge_section(ctx, phil, batch)
        stale_parts = knowledge.knowledge_section(ctx, gerry, batch)
        assert knowledge.KNOWLEDGE_STATUS_LINE not in "\n".join(fresh_parts)
        assert knowledge.KNOWLEDGE_STATUS_LINE in "\n".join(stale_parts)
        # parts[3:] is the block-and-separator tail past header/framing/blank
        # (the existing budget test's own slice) — the only bytes `used`
        # actually counts against the budget inside knowledge_section.
        fresh_bytes = sum(len(p.encode("utf-8")) for p in fresh_parts[3:] if p)
        stale_bytes = sum(len(p.encode("utf-8")) for p in stale_parts[3:] if p)
        extra = ("\n\n" + knowledge.KNOWLEDGE_STATUS_LINE).encode("utf-8")
        assert stale_bytes - fresh_bytes == len(extra)

        # A budget too tight for the status line's extra bytes truncates the
        # stale block short of the snippet it fit whole at the larger budget
        # — proof the status line spends from the same budget as everything
        # else, not a free addition on top of it.
        gerry.knowledge_bytes = stale_bytes - 1
        truncated = knowledge.knowledge_section(ctx, gerry, batch)
        truncated_bytes = sum(len(p.encode("utf-8")) for p in truncated[3:] if p)
        assert truncated_bytes <= stale_bytes - 1
        assert "\n".join(truncated) != "\n".join(stale_parts)


class TestDreamSweep:
    def _config(self, ctx):
        return load_rig_config(ctx.config_path)

    def dreaming_roster(self, ctx, budget=None):
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        if budget is not None:
            phil.knowledge_bytes = budget
        return roster

    def test_off_members_are_never_distilled(self, ctx, monkeypatch):
        calls = []
        monkeypatch.setattr(
            knowledge, "_run_k7e",
            lambda home, *a, **k: calls.append(a) or completed(),
        )
        state.write_turn_capture(NODE, "gerry", "20260730T000000000000Z", "t", "x")
        roster = load_roster(ctx.roster_path)
        assert knowledge.dream_sweep(ctx, roster, self._config(ctx)) == []
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
        config = self._config(ctx)
        assert knowledge.dream_sweep(ctx, roster, config) == ["Phil"]
        (call,) = calls
        assert call[0] == "distill" and len(call) == 3
        mark = knowledge.store_home(NODE, "phil") / ".dreamed"
        assert mark.read_text(encoding="utf-8").strip().startswith(
            "20260730T000000000002Z"
        )
        assert "DREAM phil distilled 2" in read_log()
        # Nothing new: the next sweep is a no-op.
        calls.clear()
        assert knowledge.dream_sweep(ctx, roster, config) == []
        assert calls == []

    def test_a_successful_dream_still_reports_the_files_it_could_not_read(
        self, ctx, monkeypatch
    ):
        """The watermark advances past a skipped capture, so the skip never
        comes back. A dream that succeeds is exactly where it would otherwise
        pass unseen — the failure paths already log."""
        monkeypatch.setattr(
            knowledge, "_run_k7e",
            lambda home, *a, **k: completed(
                stdout="  [stored] K7E-000-00001 A real note\n"
                       "  [skipped] /caps/bad.md: UnicodeDecodeError: bad byte\n"
            ),
        )
        state.write_turn_capture(NODE, "phil", "20260730T000000000004Z", "t", "x")
        roster = self.dreaming_roster(ctx)
        assert knowledge.dream_sweep(ctx, roster, self._config(ctx)) == ["Phil"]
        log = read_log()
        assert "DREAM phil distilled 1" in log
        assert "DREAM-SKIPPED phil /caps/bad.md: UnicodeDecodeError" in log
        assert "A real note" not in log

    def test_failure_leaves_the_watermark(self, ctx, monkeypatch):
        monkeypatch.setattr(
            knowledge, "_run_k7e",
            lambda *a, **k: completed(returncode=1, stderr="k7e distill requires an LLM command."),
        )
        state.write_turn_capture(NODE, "phil", "20260730T000000000003Z", "t", "x")
        roster = self.dreaming_roster(ctx)
        assert knowledge.dream_sweep(ctx, roster, self._config(ctx)) == []
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
        config = self._config(ctx)
        knowledge.dream_sweep(ctx, roster, config)
        (call,) = calls
        assert len(call) - 1 == knowledge.DREAM_BATCH  # "distill" + bounded paths
        calls.clear()
        knowledge.dream_sweep(ctx, roster, config)  # the remainder rides the next pass
        (call,) = calls
        assert len(call) - 1 == 3

    def test_backlog_survives_an_absent_ollama(self, ctx):
        """A real store and a dead endpoint: the queue keeps its entries, the
        day log names the backlog, and nothing about it reaches a wake."""
        seed_store("phil", "Deploy key limits", "The deploy key cannot push workflow files.")
        roster = self.dreaming_roster(ctx)
        assert knowledge.dream_sweep(ctx, roster, self._config(ctx)) == []
        assert "DREAM-EMBED-SKIP phil 1 entry still queued" in read_log()

    def test_embedded_backlog_is_priced_per_entry(self, ctx, monkeypatch):
        seed_store("phil", "Deploy key limits", "The deploy key cannot push workflow files.")
        monkeypatch.setattr(
            knowledge, "_run_k7e",
            lambda home, *a, **k: completed(
                stdout='{"embedded": 3, "pending": 0, "seconds": 0.4}'
            ),
        )
        roster = self.dreaming_roster(ctx)
        knowledge.dream_sweep(ctx, roster, self._config(ctx))
        log = read_log()
        assert "DREAM-EMBED phil embedded 3 entries in" in log
        assert "ms each)" in log

    def test_a_store_with_nothing_queued_says_nothing(self, ctx):
        seed_store("phil", "Deploy key limits", "The deploy key cannot push workflow files.")
        home = knowledge.store_home(NODE, "phil")
        res = knowledge._run_k7e(home, "reindex")  # drops the pending queue
        assert res.returncode == 0
        roster = self.dreaming_roster(ctx)
        knowledge.dream_sweep(ctx, roster, self._config(ctx))
        assert "DREAM-EMBED" not in read_log()

    def test_run_idle_reports_dreaming(self, ctx):
        result = dispatch.run_idle(ctx)
        assert result["dreamed"] == []


class TestDistillRigResolution:
    """Dreaming's distill rig (#52): the `Knowledge:` line's rig-name override
    when present, else the member's own turn rig (the K2 verdict)."""

    def test_defaults_to_the_members_own_rig(self, ctx):
        config = load_rig_config(ctx.config_path)
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        rig, err = knowledge.resolve_distill_rig(phil, config)
        assert err is None
        assert rig is config.rigs["junior-dev"]

    def test_knowledge_line_rig_overrides(self, ctx):
        config = load_rig_config(ctx.config_path)
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        phil.knowledge_distill_rig = "leader"
        rig, err = knowledge.resolve_distill_rig(phil, config)
        assert err is None
        assert rig is config.rigs["leader"]

    def test_unknown_override_rig_is_an_error(self, ctx):
        config = load_rig_config(ctx.config_path)
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        phil.knowledge_distill_rig = "ghost"
        rig, err = knowledge.resolve_distill_rig(phil, config)
        assert rig is None
        assert "ghost" in err and "not found" in err

    def test_dream_sweep_skips_and_logs_an_unresolved_distill_rig(self, ctx):
        state.write_turn_capture(NODE, "phil", "20260730T000000000009Z", "t", "x")
        config = load_rig_config(ctx.config_path)
        roster = load_roster(ctx.roster_path)
        phil = roster.find("phil")
        phil.knowledge_on = True
        phil.knowledge_distill_rig = "ghost"
        assert knowledge.dream_sweep(ctx, roster, config) == []
        log = read_log()
        assert "DREAM-SKIP phil" in log and "ghost" in log


class TestDistillCommandBridge:
    """dream_sweep bridges the resolved distill rig to k7e as
    K7E_DISTILL_COMMAND (#52)."""

    def test_extra_env_carries_the_resolved_rigs_command(self, ctx, monkeypatch):
        config = load_rig_config(ctx.config_path)
        captured = {}

        def fake_run(home, *args, timeout=60, extra_env=None):
            captured["extra_env"] = extra_env
            return completed(stdout="ok")

        monkeypatch.setattr(knowledge, "_run_k7e", fake_run)
        state.write_turn_capture(NODE, "phil", "20260730T000000000010Z", "t", "x")
        roster = self.dreaming_roster(ctx)
        assert knowledge.dream_sweep(ctx, roster, config) == ["Phil"]
        assert "K7E_DISTILL_COMMAND" in captured["extra_env"]

    def dreaming_roster(self, ctx):
        roster = load_roster(ctx.roster_path)
        roster.find("phil").knowledge_on = True
        return roster
