"""The node ticker — one stdout line per lifecycle event.

a8s pumps a wake subprocess's stdout into the node's log as the lines arrive,
so these lines ARE `a8s logs <node> -f`. Two properties matter and both are
asserted here: one line per event, and never a message body.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

import state
from dispatch import DispatchContext, drain, handle_message
from r4t import main as r4t_main
from rig import load_rig_config
from roster import load_roster

NODE = "acme"
SENTINEL = "vault-passphrase-hunter2-do-not-log-this"


def ticker_lines(capsys) -> list[str]:
    """Only the ticker's own lines: `r4t: <EVENT> <subject> ...`."""
    out = capsys.readouterr().out
    return [ln for ln in out.splitlines() if ln.startswith("r4t: ")]


def events(capsys, name: str) -> list[str]:
    return [ln for ln in ticker_lines(capsys) if ln.startswith(f"r4t: {name} ")]


def run_one(ctx, sender, to, message, run_fn):
    handle_message(ctx, sender, to, message, run_fn=run_fn, drain_after=False)
    return drain(ctx, run_fn=run_fn)


def empty_member_budget(ctx, name):
    config = load_rig_config(ctx.config_path)
    member = load_roster(ctx.roster_path).find(name)
    rig, _e, _p = config.rig_for(member)
    state.budget_charge(
        NODE, name, rig.budget_max, rig.budget_earn_per_hour, rig.budget_max + 5
    )


def ok_turn(rig, prompt, workdir, *, env=None, variant=0):
    return 0, "fake harness ran", 1.5, False


def failed_turn(rig, prompt, workdir, *, env=None, variant=0):
    return 1, "boom", 0.5, False


@pytest.fixture
def ticker_ctx(ctx):
    """The wake entry points (`r4t dispatch`, `r4t idle`) build their context
    with the ticker on; every other caller leaves it off."""
    return replace(ctx, ticker=True)


class TestTurnBoundaries:
    def test_turn_start_emits_exactly_one_line(self, ticker_ctx, capsys):
        run_one(ticker_ctx, "boss", "acme", "ship it", ok_turn)
        turns = events(capsys, "TURN")
        assert len(turns) == 1
        head, size = turns[0].rsplit(" ", 1)
        assert head == "r4t: TURN gerry 1 msg rig=leader founding"
        assert re.fullmatch(r"prompt=\d+\.\d+k", size)

    def test_turn_start_names_member_batch_size_and_rig(self, ticker_ctx, capsys):
        handle_message(
            ticker_ctx, "boss", "acme", "one", run_fn=ok_turn, drain_after=False
        )
        handle_message(
            ticker_ctx, "boss", "acme", "two", run_fn=ok_turn, drain_after=False
        )
        drain(ticker_ctx, run_fn=ok_turn)
        (turn,) = events(capsys, "TURN")
        assert turn.split()[2:5] == ["gerry", "2", "msg"]
        assert "rig=leader" in turn

    def test_turn_end_emits_exactly_one_line_with_outcome_and_duration(
        self, ticker_ctx, capsys
    ):
        run_one(ticker_ctx, "boss", "acme", "ship it", ok_turn)
        done = events(capsys, "DONE")
        assert done == ["r4t: DONE gerry exit 0 in 1.5s"]

    def test_turn_end_reports_a_failure_and_its_requeue(self, ticker_ctx, capsys):
        run_one(ticker_ctx, "boss", "acme", "ship it", failed_turn)
        (done,) = events(capsys, "DONE")
        assert done == "r4t: DONE gerry exit 1 in 0.5s — 1 msg requeued"

    def test_start_precedes_end(self, ticker_ctx, capsys):
        run_one(ticker_ctx, "boss", "acme", "ship it", ok_turn)
        names = [ln.split()[1] for ln in ticker_lines(capsys)]
        assert names == ["QUEUED", "TURN", "DONE"]

    def test_a_turn_that_never_starts_narrates_nothing(self, ticker_ctx, capsys):
        assert drain(ticker_ctx, run_fn=ok_turn) == 0
        assert ticker_lines(capsys) == []


class TestIngest:
    def test_each_message_emits_one_queued_line(self, ticker_ctx, capsys):
        handle_message(
            ticker_ctx, "boss", "acme", "one", run_fn=ok_turn, drain_after=False
        )
        handle_message(
            ticker_ctx, "boss", "acme", "two", run_fn=ok_turn, drain_after=False
        )
        queued = events(capsys, "QUEUED")
        assert len(queued) == 2
        assert queued[0].startswith("r4t: QUEUED gerry from boss thread=")
        assert queued[0].endswith("hop=0 depth=1")
        assert queued[1].endswith("hop=0 depth=2")

    def test_subject_is_the_member_not_the_sender(self, ticker_ctx, capsys):
        handle_message(
            ticker_ctx, "gerry", "acme", "hi", run_fn=ok_turn, drain_after=False
        )
        (queued,) = events(capsys, "QUEUED")
        assert queued.split()[2] == "gerry"


class TestBlocks:
    def test_a_resting_member_emits_its_reason(self, ticker_ctx, capsys):
        empty_member_budget(ticker_ctx, "Gerry")
        run_one(ticker_ctx, "boss", "acme", "ship it", ok_turn)
        (resting,) = events(capsys, "RESTING")
        assert resting.startswith("r4t: RESTING gerry resting (member budget ")
        assert resting.endswith("(1 queued)")
        assert events(capsys, "TURN") == []

    def test_an_open_breaker_emits_its_reason(self, ticker_ctx, capsys):
        config = load_rig_config(ticker_ctx.config_path)
        state.update_meta(
            NODE, "Gerry",
            consecutive_failures=config.breaker_cap,
            last_failure_at=state.utc_now(),
        )
        run_one(ticker_ctx, "boss", "acme", "ship it", ok_turn)
        (breaker,) = events(capsys, "BREAKER")
        assert breaker.startswith("r4t: BREAKER gerry breaker open (")
        assert breaker.endswith("(1 queued)")

    def test_the_cadence_throttle_emits_its_reason(
        self, ctx, tmp_path, rig_config, capsys
    ):
        config = json.loads(rig_config.read_text(encoding="utf-8"))
        config["throttle"]["min_seconds_between_turn_starts"] = 900
        slow = tmp_path / "slow-rigs.json"
        slow.write_text(json.dumps(config), encoding="utf-8")
        slow_ctx = replace(ctx, config_path=slow, ticker=True)

        state.stamp_last_turn_start(NODE)
        handle_message(
            slow_ctx, "boss", "acme", "ship it", run_fn=ok_turn, drain_after=False
        )
        drain(slow_ctx, run_fn=ok_turn)
        (deferred,) = events(capsys, "DEFERRED")
        assert deferred.startswith("r4t: DEFERRED gerry roster throttle: last turn started ")
        assert "min_seconds_between_turn_starts 900" in deferred
        assert deferred.endswith("(1 queued)")


class TestNoBodies:
    def test_no_line_carries_the_message_body(self, ticker_ctx, capsys):
        run_one(ticker_ctx, "boss", "acme", SENTINEL, ok_turn)
        out = capsys.readouterr().out
        assert SENTINEL not in out
        # ...and the archive DID keep it, so the assertion above is about the
        # ticker's discipline, not about the message going missing.
        log = "".join(
            f.read_text(encoding="utf-8")
            for f in (state.roster_dir(NODE) / "log").glob("*.md")
        )
        assert SENTINEL in log

    def test_no_line_carries_transcript_output(self, ticker_ctx, capsys):
        def loud(rig, prompt, workdir, *, env=None, variant=0):
            return 0, SENTINEL * 20, 1.0, False

        run_one(ticker_ctx, "boss", "acme", "ship it", loud)
        assert SENTINEL not in capsys.readouterr().out

    def test_every_event_is_a_single_line(self, ticker_ctx, capsys):
        run_one(ticker_ctx, "boss", "acme", f"line one\nline two\n{SENTINEL}", ok_turn)
        lines = capsys.readouterr().out.splitlines()
        assert lines and all(ln.startswith("r4t: ") for ln in lines)


class TestOffByDefault:
    def test_a_plain_context_narrates_nothing(self, ctx, capsys):
        assert ctx.ticker is False
        run_one(ctx, "boss", "acme", "ship it", ok_turn)
        assert capsys.readouterr().out == ""

    def test_the_day_log_is_written_either_way(self, ctx, capsys):
        run_one(ctx, "boss", "acme", "ship it", ok_turn)
        capsys.readouterr()
        log = "".join(
            f.read_text(encoding="utf-8")
            for f in (state.roster_dir(NODE) / "log").glob("*.md")
        )
        assert "r4t: QUEUED boss -> gerry" in log
        assert "r4t: PROMPT gerry founding" in log


class TestWakeEntryPoints:
    """`dispatch` and `idle` are the two verbs a8s runs as wake subprocesses,
    and they are where the ticker is on: their stdout IS the node's log."""

    def cli(self, ctx, *args):
        return r4t_main([
            *args, "--root", str(ctx.root),
            "--rig-config", str(ctx.config_path), "--no-notify",
        ])

    def test_dispatch_narrates(self, ctx, capsys, fake_harness):
        assert self.cli(
            ctx, "dispatch", "--from", "boss", "--to", "acme", "--message", SENTINEL
        ) == 0
        out = capsys.readouterr().out
        assert "r4t: QUEUED gerry from boss" in out
        assert "r4t: TURN gerry 1 msg rig=leader" in out
        assert "r4t: DONE gerry exit 0 in " in out
        assert SENTINEL not in out

    def test_batch_dispatch_narrates_every_arrival(self, ctx, capsys, fake_harness):
        batch = json.dumps([
            {"from": "boss", "to": "acme", "content": "one"},
            {"from": "boss", "to": "acme", "content": "two"},
        ])
        assert self.cli(ctx, "dispatch", "--batch", batch) == 0
        out = capsys.readouterr().out
        assert out.count("r4t: QUEUED gerry from boss") == 2
        assert out.count("r4t: TURN gerry ") == 1

    def test_an_idle_wake_with_an_empty_queue_narrates_nothing(
        self, ctx, capsys, fake_harness
    ):
        assert self.cli(ctx, "idle", "--node", NODE) == 0
        out = capsys.readouterr().out
        assert [ln for ln in out.splitlines() if ln.startswith("r4t: ")] == []


class TestContextEvent:
    def test_rest_is_optional(self, repo, rig_config, capsys):
        ctx = DispatchContext(
            root=repo,
            node=NODE,
            roster_path=repo / "ROSTER.md",
            config_path=rig_config,
            tell_fn=lambda a, b: None,
            ticker=True,
        )
        ctx.event("TURN", "gerry")
        assert capsys.readouterr().out == "r4t: TURN gerry\n"
