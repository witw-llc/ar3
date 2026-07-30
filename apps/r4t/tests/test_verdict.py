"""Verdict engine tests — plain-English health lines and dead-letter rollup."""
from __future__ import annotations

import time

import pytest

import state
import tasks
import verdict
from rig import load_rig_config
from roster import load_roster
from ulid import new as new_ulid

NODE = "acme"


@pytest.fixture
def roster(repo):
    return load_roster(repo / "ROSTER.md")


@pytest.fixture
def config(rig_config):
    return load_rig_config(rig_config)


def open_task(creator: str = "acme:neil", **fields) -> dict:
    task = tasks.ensure_task(NODE, new_ulid(), creator)
    task.update(fields)
    tasks.save_task(NODE, task)
    return task


def by_level(verdicts, level):
    return [v for v in verdicts if v.level == level]


def texts(verdicts):
    return "\n".join(v.text for v in verdicts)


def enqueue_n(name: str, n: int) -> None:
    for i in range(n):
        state.enqueue(NODE, name, {"from": f"sender-{i}", "body": f"job {i}", "thread": "t"})


def empty_budget(node, key, budget_max, earn):
    state.budget_charge(node, key, budget_max, earn, budget_max + 10)


class TestRollup:
    def test_routine_vs_signals(self):
        records = [
            {"reason": "quota", "from": "a", "to": "b"},
            {"reason": "quota", "from": "a", "to": "b"},
            {"reason": "unknown-recipient", "from": "a", "to": "ghost"},
            {"reason": "no-rig", "from": "a", "to": "b"},
        ]
        roll = verdict.rollup_dead_letters(records)
        assert roll.routine == {"quota": 2}
        assert roll.routine_total == 2
        assert set(roll.signals) == {"unknown-recipient", "no-rig"}
        assert roll.signal_total == 2

    def test_empty(self):
        roll = verdict.rollup_dead_letters([])
        assert roll.routine_total == 0 and roll.signal_total == 0


class TestSeatVerdict:
    def test_unread_waits_on_you(self, r4t_home, roster, config):
        state.park_seat_message(NODE, "Neil", "acme:gerry", "look at this")
        verdicts = verdict.team_verdicts(NODE, roster, config)
        bad = by_level(verdicts, verdict.BAD)
        assert any("waiting on YOU" in v.text for v in bad)
        assert any("seat inbox" in (v.hint or "") for v in bad)

    def test_quiet_seat_is_ok(self, r4t_home, roster, config):
        verdicts = verdict.team_verdicts(NODE, roster, config)
        assert any("nothing waiting on you" in v.text for v in verdicts)

    def test_no_roster_skips_seat(self, r4t_home):
        verdicts = verdict.team_verdicts(NODE, None, None)
        assert "waiting on you" not in texts(verdicts)


class TestRunawayVerdict:
    def test_quiet_team_no_runaway(self, r4t_home, roster, config):
        verdicts = verdict.team_verdicts(NODE, roster, config)
        assert any("no runaway signs" in v.text for v in verdicts)

    def test_high_turn_rate_warns(self, r4t_home, roster, config):
        for _ in range(verdict.RUNAWAY_TURNS_PER_WINDOW):
            state.record_velocity(
                NODE, agent="phil", rig="junior-dev", thread="01X",
                hop=1, duration_seconds=1.0, exit_code=0,
            )
        verdicts = verdict.team_verdicts(NODE, roster, config)
        warn = by_level(verdicts, verdict.WARN)
        assert any("hot:" in v.text for v in warn)
        assert not any("no runaway signs" in v.text for v in verdicts)

    def test_old_turns_do_not_count(self, r4t_home, roster, config):
        for _ in range(verdict.RUNAWAY_TURNS_PER_WINDOW):
            state.record_velocity(
                NODE, agent="phil", rig="junior-dev", thread="01X",
                hop=1, duration_seconds=1.0, exit_code=0,
            )
        later = time.time() + verdict.RECENT_WINDOW_SECONDS + 60
        verdicts = verdict.team_verdicts(NODE, roster, config, now=later)
        assert any("no runaway signs" in v.text for v in verdicts)

    def test_cell_budget_spent_warns(self, r4t_home, roster, config):
        empty_budget(
            NODE, state.CELL_BUDGET_KEY,
            config.cell_budget_max, config.cell_budget_earn_per_hour,
        )
        verdicts = verdict.team_verdicts(NODE, roster, config)
        warn = by_level(verdicts, verdict.WARN)
        assert any("cell budget spent" in v.text for v in warn)


class TestMemberVerdicts:
    def test_all_healthy(self, r4t_home, roster, config):
        verdicts = verdict.team_verdicts(NODE, roster, config)
        assert any("member(s) healthy" in v.text for v in verdicts)

    def test_breaker_open_is_bad(self, r4t_home, roster, config):
        state.update_meta(
            NODE, "phil",
            consecutive_failures=config.breaker_cap,
            last_failure_at=state.utc_now(),
        )
        verdicts = verdict.team_verdicts(NODE, roster, config)
        bad = by_level(verdicts, verdict.BAD)
        assert any("Phil broken" in v.text for v in bad)
        assert not any("member(s) healthy" in v.text for v in verdicts)

    def test_resting_member_with_queue_warns(self, r4t_home, roster, config):
        rig, _e, _p = config.rig_for(next(m for m in roster.members if m.name == "Phil"))
        enqueue_n("phil", 2)
        empty_budget(NODE, "phil", rig.budget_max, rig.budget_earn_per_hour)
        verdicts = verdict.team_verdicts(NODE, roster, config)
        warn = by_level(verdicts, verdict.WARN)
        assert any("Phil resting" in v.text and "queued" in v.text for v in warn)

    def test_resting_without_queue_is_quiet(self, r4t_home, roster, config):
        rig, _e, _p = config.rig_for(next(m for m in roster.members if m.name == "Phil"))
        empty_budget(NODE, "phil", rig.budget_max, rig.budget_earn_per_hour)
        verdicts = verdict.team_verdicts(NODE, roster, config)
        assert "resting" not in texts(verdicts)

    def test_deep_queue_backs_up(self, r4t_home, roster, config):
        enqueue_n("phil", verdict.QUEUE_DEPTH_WARN)
        verdicts = verdict.team_verdicts(NODE, roster, config)
        warn = by_level(verdicts, verdict.WARN)
        assert any("backing up" in v.text for v in warn)


class TestSignalDeadLetters:
    def test_recent_signal_warns_with_gloss(self, r4t_home, roster, config):
        state.record_dead_letter(
            NODE, reason="unknown-recipient", sender="acme:gerry", to="ghost",
            thread="", content="x",
        )
        verdicts = verdict.team_verdicts(NODE, roster, config)
        warn = by_level(verdicts, verdict.WARN)
        assert any(
            "unknown-recipient" in v.text and "not on the roster" in v.text
            for v in warn
        )

    def test_stale_signal_is_quiet(self, r4t_home, roster, config):
        state.record_dead_letter(
            NODE, reason="unknown-recipient", sender="acme:gerry", to="ghost",
            thread="", content="x",
        )
        later = time.time() + verdict.SIGNAL_RECENT_SECONDS + 60
        verdicts = verdict.team_verdicts(NODE, roster, config, now=later)
        assert "unknown-recipient" not in texts(verdicts)


def test_worst_level():
    v = verdict.Verdict
    assert verdict.worst_level([v("ok", "a")]) == "ok"
    assert verdict.worst_level([v("ok", "a"), v("warn", "b")]) == "warn"
    assert verdict.worst_level([v("warn", "a"), v("bad", "b")]) == "bad"
    assert verdict.worst_level([]) == "ok"
