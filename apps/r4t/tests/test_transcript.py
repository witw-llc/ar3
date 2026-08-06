"""Tests for the conversation probes and the continuation limits.

The probe reads files another program wrote, so the fixtures here build those
files in the shape that program actually uses. `_claude_session` mirrors a real
Claude Code JSONL: one object per line, usage on the assistant messages.
"""
from __future__ import annotations

import json

import pytest

import transcript
from rig import Rig


def _claude_session(
    home,
    workdir,
    *,
    name="session.jsonl",
    read=0,
    created=0,
    fresh=0,
    ephemeral_1h=0,
    ephemeral_5m=0,
    padding=0,
    trailing_junk=False,
):
    slug = str(workdir).replace("/", "-").replace(".", "-")
    folder = home / "projects" / slug
    folder.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "user", "message": {"role": "user", "content": "x" * padding}}]
    rows.append({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "usage": {
                "input_tokens": fresh,
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": created,
                "cache_creation": {
                    "ephemeral_1h_input_tokens": ephemeral_1h,
                    "ephemeral_5m_input_tokens": ephemeral_5m,
                },
            },
        },
    })
    lines = [json.dumps(r) for r in rows]
    if trailing_junk:
        # Claude Code appends summary/meta rows after the last usage row, and a
        # crashed run can leave a half-written line. Neither may hide the usage.
        lines.append(json.dumps({"type": "summary", "summary": "done"}))
        lines.append('{"type": "assistant", "mess')
    path = folder / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    home = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


class TestClaudeProbe:
    def test_reads_the_newest_turn(self, claude_home, tmp_path):
        work = tmp_path / "repo"
        _claude_session(
            claude_home, work,
            read=273_865, created=4_961, fresh=2, ephemeral_1h=4_961,
        )
        convo = transcript.probe("claude", work)
        assert convo is not None
        assert convo.cache_read_tokens == 273_865
        assert convo.cache_creation_tokens == 4_961
        assert convo.ephemeral_1h_tokens == 4_961
        assert convo.ephemeral_5m_tokens == 0
        # What the next continuation would carry before adding anything.
        assert convo.context_tokens == 273_865 + 4_961 + 2
        assert convo.measured

    def test_usage_survives_rows_written_after_it(self, claude_home, tmp_path):
        work = tmp_path / "repo"
        _claude_session(claude_home, work, read=1_000, trailing_junk=True)
        convo = transcript.probe("claude", work)
        assert convo is not None and convo.cache_read_tokens == 1_000

    def test_picks_the_session_continue_would_resume(self, claude_home, tmp_path):
        import os
        import time

        work = tmp_path / "repo"
        old = _claude_session(claude_home, work, name="old.jsonl", read=10)
        new = _claude_session(claude_home, work, name="new.jsonl", read=99)
        os.utime(old, (time.time() - 500, time.time() - 500))
        convo = transcript.probe("claude", work)
        assert convo is not None
        assert convo.path == new and convo.cache_read_tokens == 99

    def test_a_dot_in_the_path_flattens_like_a_slash(self, claude_home, tmp_path):
        # `/a/b/.claude/wt` becomes `-a-b--claude-wt`: both separators map to
        # a dash, which is why the worktree folders show a double dash.
        work = tmp_path / ".hidden" / "repo"
        _claude_session(claude_home, work, read=7)
        convo = transcript.probe("claude", work)
        assert convo is not None and convo.cache_read_tokens == 7

    def test_a_transcript_with_no_completed_turn_is_unmeasured(
        self, claude_home, tmp_path
    ):
        work = tmp_path / "repo"
        slug = str(work).replace("/", "-").replace(".", "-")
        folder = claude_home / "projects" / slug
        folder.mkdir(parents=True)
        (folder / "s.jsonl").write_text('{"type": "user"}\n', encoding="utf-8")
        convo = transcript.probe("claude", work)
        # Found, sized, but nothing to report — the byte cap can still act.
        assert convo is not None and not convo.measured
        assert convo.size_bytes > 0

    def test_no_transcript_is_no_answer(self, claude_home, tmp_path):
        assert transcript.probe("claude", tmp_path / "never-run") is None

    def test_an_unmeasured_harness_has_no_probe(self, tmp_path):
        # agy, codex, cursor: continuation works, cache behaviour unresearched.
        assert transcript.probe("agy", tmp_path) is None
        assert transcript.probe(None, tmp_path) is None


class TestOverLimit:
    def _convo(self, tmp_path, tokens=0, size=0):
        return transcript.Conversation(
            path=tmp_path / "s.jsonl",
            size_bytes=size,
            context_tokens=tokens,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            ephemeral_1h_tokens=0,
            ephemeral_5m_tokens=0,
        )

    def test_tokens_over_cap(self, tmp_path):
        reason = self._convo(tmp_path, tokens=200_000).over_limit(180_000, None)
        assert reason and "200000 tokens" in reason

    def test_bytes_are_the_fallback_when_usage_is_missing(self, tmp_path):
        # No usage rows means no token count; size on disk is all that is left.
        reason = self._convo(tmp_path, tokens=0, size=9_000_000).over_limit(
            180_000, 4 * 1024 * 1024
        )
        assert reason and "bytes" in reason

    def test_under_both_caps_is_fine(self, tmp_path):
        assert self._convo(tmp_path, tokens=50, size=50).over_limit(
            180_000, 4 * 1024 * 1024
        ) is None

    def test_no_caps_never_trips(self, tmp_path):
        assert self._convo(tmp_path, tokens=10**9, size=10**9).over_limit(
            None, None
        ) is None


class TestPresetLimits:
    def test_claude_carries_all_three(self):
        rig = Rig(name="r", preset="claude")
        assert rig.continue_warm_seconds == 270
        assert rig.continue_max_context_tokens == 180_000
        assert rig.continue_max_transcript_bytes == 4 * 1024 * 1024

    @pytest.mark.parametrize("preset", ["agy", "codex", "cursor", "opencode"])
    def test_an_unmeasured_preset_is_not_gated(self, preset):
        # Continuation still works there; it is simply not priced yet, and a
        # guessed window would cost money on purpose.
        rig = Rig(name="r", preset=preset)
        assert rig.continue_warm_seconds is None
        assert rig.continue_max_context_tokens is None
        assert rig.continue_max_transcript_bytes is None

    def test_the_claude_preset_stabilizes_its_prefix(self):
        from rig import HARNESS_PRESETS

        assert (
            "--exclude-dynamic-system-prompt-sections"
            in HARNESS_PRESETS["claude"]["invoke"]
        )
