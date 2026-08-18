"""The rotation: who goes next, and why the answer is a printable one.

Selection is a pure function of the inbox directories plus two meta fields, so
these tests state inbox conditions and read back the choice. Nothing here mocks
a clock: the score is integers and the aging is counted in turns, which is the
whole reason it can be tested this way.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from dataclasses import replace
from pathlib import Path

import pytest

import dispatch
import schedule
import state
from dispatch import drain, handle_message, run_idle
from org import ORG_CONFIG_NAME
from rig import load_rig_config
from roster import load_roster
from r4t import main as r4t_main

NODE = "acme"


def look(ctx, *, priority_senders=()):
    return schedule.snapshot(
        NODE,
        load_rig_config(ctx.config_path),
        load_roster(ctx.roster_path),
        priority_senders=priority_senders,
    )


def queue(name, sender="boss", *, origin="intra", body="work"):
    state.enqueue(
        NODE, name,
        {"from": sender, "to": f"{NODE}:{name.lower()}", "thread": "t", "hop": 0,
         "class": "human", "origin": origin, "body": body},
    )


def entry_for(entries, name):
    return next(e for e in entries if e.member.lower() == name.lower())


def read_log():
    files = (state.roster_dir(NODE) / "log").glob("*.md")
    return "".join(f.read_text(encoding="utf-8") for f in files)


def ok_turn(rig, prompt, workdir, *, env=None, variant=0):
    return 0, "fake harness ran", 0.1, False


def missing_binary_turn(rig, prompt, workdir, *, env=None, variant=0):
    return (
        127,
        "failed to spawn harness 'nowhere-cli': [Errno 2] No such file or "
        "directory: 'nowhere-cli'",
        0.0,
        False,
    )


class TestScoreTable:
    """The score is `2*ask + 1*ingress + passes`, and every row of that table
    is checkable by hand off one status line. `ask` does not exist yet and is
    always 0."""

    @pytest.mark.parametrize(
        "ingress,passes,expected",
        [
            (False, 0, 0),
            (True, 0, 1),
            (False, 1, 1),
            (True, 2, 3),
            (False, 4, 4),
            (True, 4, 5),
        ],
    )
    def test_score_is_the_sum_of_its_named_terms(self, ingress, passes, expected):
        entry = schedule.RunEntry(
            member="mira", depth=1, oldest_ns=1, repeats=1,
            has_ingress=ingress, has_ask=False, priority_from="",
            passes=passes, state=schedule.READY, reason="",
        )
        assert entry.score == expected

    def test_ask_is_reserved_and_contributes_nothing_today(self, ctx):
        queue("Phil")
        (entry,) = look(ctx)
        assert entry.has_ask is False
        assert schedule.ASK_WEIGHT == 2  # the slot the future verb lands in

    def test_the_why_names_the_terms_before_the_number(self, ctx):
        queue("Phil", origin="ingress")
        state.update_meta(NODE, "Phil", passes=2)
        (entry,) = look(ctx)
        assert entry.why == "ingress + passed over 2"
        assert entry.score == 3

    def test_intra_mail_says_so_rather_than_saying_nothing(self, ctx):
        queue("Phil")
        (entry,) = look(ctx)
        assert entry.why == "intra"


class TestSelectionIsDeterministic:
    def test_ingress_outranks_intra_at_equal_age(self, ctx):
        queue("Gerry")
        queue("Phil", origin="ingress")
        assert schedule.next_up(look(ctx)).member.lower() == "phil"

    def test_ties_break_by_oldest_message_then_by_name(self, ctx):
        queue("Phil")
        time.sleep(0.002)
        queue("Gerry")
        entries = look(ctx)
        assert [e.member.lower() for e in entries] == ["phil", "gerry"]

    def test_the_same_inboxes_always_give_the_same_answer(self, ctx):
        queue("Gerry", origin="ingress")
        queue("Phil")
        first = [(e.member, e.score) for e in look(ctx)]
        for _ in range(5):
            assert [(e.member, e.score) for e in look(ctx)] == first

    def test_a_member_with_no_mail_is_not_in_the_rotation(self, ctx):
        queue("Phil")
        assert [e.member.lower() for e in look(ctx)] == ["phil"]

    def test_next_up_skips_what_the_caller_already_tried(self, ctx):
        queue("Gerry", origin="ingress")
        queue("Phil")
        assert schedule.next_up(look(ctx), skip=["gerry"]).member.lower() == "phil"

    def test_next_up_is_none_when_nothing_is_ready(self, ctx):
        assert schedule.next_up(look(ctx)) is None


class TestPriorityTier:
    def test_a_priority_sender_goes_next_whatever_the_score(self, ctx):
        queue("Gerry", origin="ingress")
        state.update_meta(NODE, "Gerry", passes=9)  # a huge tier-2 score
        queue("Phil", "neil@phone")
        entries = look(ctx, priority_senders=["neil*"])
        assert schedule.next_up(entries).member.lower() == "phil"
        assert entry_for(entries, "gerry").score == 10  # and it was outranked anyway

    def test_the_glob_matches_case_insensitively(self, ctx):
        queue("Phil", "Neil@Phone")
        (entry,) = look(ctx, priority_senders=["neil*"])
        assert entry.priority is True
        assert entry.why == "PRIORITY (Neil@Phone) — always next"

    def test_an_empty_glob_list_leaves_a_pure_score_rotation(self, ctx):
        queue("Phil", "neil")
        (entry,) = look(ctx, priority_senders=[])
        assert entry.priority is False

    def test_among_several_priority_members_the_oldest_goes_first(self, ctx):
        queue("Phil", "neil")
        time.sleep(0.002)
        queue("Gerry", "neil")
        entries = look(ctx, priority_senders=["neil*"])
        assert [e.member.lower() for e in entries] == ["phil", "gerry"]

    def test_priority_never_preempts_a_running_turn(self, ctx, fake_harness):
        live = state.AgentLock(NODE, "gerry")
        assert live.acquire("leader")
        queue("Phil", "neil")
        assert drain(replace(ctx, priority_senders=["neil*"]), run_fn=ok_turn) == 0
        assert state.queue_depth(NODE, "phil") == 1  # next, not now


class TestStarvation:
    """Class advantage is worth at most ASK_WEIGHT + INGRESS_WEIGHT turns of
    waiting, so after STARVATION_BOUND passes nothing that just arrived can
    outrank you."""

    def test_the_bound_is_one_more_than_every_class_combined(self):
        assert schedule.STARVATION_BOUND == (
            schedule.ASK_WEIGHT + schedule.INGRESS_WEIGHT + 1
        ) == 4

    def test_four_passes_outrank_any_freshly_arrived_class(self, ctx):
        queue("Gerry", origin="ingress")  # every advantage today's score can give
        queue("Phil")
        state.update_meta(NODE, "Phil", passes=schedule.STARVATION_BOUND)
        entries = look(ctx)
        assert schedule.next_up(entries).member.lower() == "phil"
        assert entry_for(entries, "phil").score > entry_for(entries, "gerry").score

    def test_no_ready_member_waits_more_than_four_turns(self, ctx, fake_harness):
        """Gerry keeps drawing fresh ingress mail; Phil holds one intra
        message and nothing else. Phil still runs by the fifth selection."""
        queue("Phil")
        selections = []
        for _ in range(5):
            queue("Gerry", origin="ingress")
            entries = look(ctx)
            chosen = schedule.next_up(entries)
            selections.append(chosen.member.lower())
            schedule.record_selection(NODE, entries, chosen.member)
            state.claim_queue(NODE, chosen.member)
            state.release_claim(NODE, chosen.member)
            if chosen.member.lower() == "phil":
                break
        assert "phil" in selections
        assert len(selections) <= schedule.STARVATION_BOUND + 1

    def test_passes_rise_for_the_skipped_and_reset_for_the_chosen(self, ctx):
        queue("Gerry", origin="ingress")
        queue("Phil")
        state.update_meta(NODE, "Phil", passes=3)
        entries = look(ctx)
        schedule.record_selection(NODE, entries, "Phil")
        assert state.read_meta(NODE, "Phil").get("passes", 0) == 0
        assert state.read_meta(NODE, "Gerry")["passes"] == 1

    def test_a_resting_member_does_not_age(self, ctx):
        """The budget held it back, not the scheduler. Ageing it would let a
        broke member jump the queue the moment it can afford a turn."""
        config = load_rig_config(ctx.config_path)
        rig = config.rig_for(load_roster(ctx.roster_path).find("Phil"))[0]
        state.budget_charge(
            NODE, "Phil", rig.budget_max, rig.budget_earn_per_hour, rig.budget_max + 5
        )
        queue("Phil")
        queue("Gerry")
        entries = look(ctx)
        assert entry_for(entries, "phil").state == schedule.RESTING
        schedule.record_selection(NODE, entries, "Gerry")
        assert state.read_meta(NODE, "Phil").get("passes", 0) == 0


class TestQueueFacts:
    def test_depth_and_oldest_come_from_the_filenames(self, ctx):
        queue("Phil")
        queue("Phil", body="second")
        (entry,) = look(ctx)
        assert entry.depth == 2
        assert entry.oldest_ns == int(
            state.list_queue(NODE, "Phil")[0].name.split("-", 1)[0]
        )

    def test_repeats_surface_so_the_collapse_is_auditable(self, ctx):
        for _ in range(3):
            queue("Phil", body="same thing")
        (entry,) = look(ctx)
        assert entry.depth == 1 and entry.repeats == 3
        assert "1 repeated x3" in entry.queue_note

    def test_ingress_is_stamped_at_the_wall_by_ingest(self, ctx):
        handle_message(ctx, "boss", NODE, "outside mail", drain_after=False)
        (entry,) = look(ctx)
        assert entry.has_ingress is True
        assert state.read_queue(NODE, "Gerry")[0]["origin"] == "ingress"

    def test_intra_roster_release_is_not_ingress(self, ctx):
        dispatch._ingest(
            ctx, f"{NODE}:gerry", f"{NODE}:phil", "your turn",
            klass="auto", internal=True, thread="t", hop=1,
        )
        assert state.read_queue(NODE, "Phil")[0]["origin"] == "intra"
        assert entry_for(look(ctx), "phil").has_ingress is False


class TestOneFunctionTwoCallers:
    def test_status_prints_the_member_the_drain_would_pick(self, ctx, capsys):
        queue("Gerry")
        queue("Phil", origin="ingress")
        picked = schedule.next_up(look(ctx, priority_senders=["neil*"])).member.lower()
        assert r4t_main(
            ["status", "--node", NODE, "--root", str(ctx.root),
             "--rig-config", str(ctx.config_path), "--no-notify"]
        ) == 0
        out = capsys.readouterr().out
        next_line = next(ln for ln in out.splitlines() if ln.strip().startswith("Next"))
        assert picked in next_line


class TestInflightRecovery:
    def test_a_claim_moves_rather_than_deletes(self, ctx):
        queue("Phil")
        batch = state.claim_queue(NODE, "Phil")
        assert len(batch) == 1
        assert state.queue_depth(NODE, "Phil") == 0
        assert len(state.list_inflight(NODE, "Phil")) == 1

    def test_release_drops_the_claim(self, ctx):
        queue("Phil")
        state.claim_queue(NODE, "Phil")
        state.release_claim(NODE, "Phil")
        assert state.list_inflight(NODE, "Phil") == []
        assert state.queue_depth(NODE, "Phil") == 0

    def test_return_puts_it_back_under_the_same_names(self, ctx):
        queue("Phil")
        before = [p.name for p in state.list_queue(NODE, "Phil")]
        state.claim_queue(NODE, "Phil")
        assert state.return_claim(NODE, "Phil") == 1
        assert [p.name for p in state.list_queue(NODE, "Phil")] == before

    def test_a_live_lock_protects_its_in_flight_batch(self, ctx):
        queue("Phil")
        state.claim_queue(NODE, "Phil")
        lock = state.AgentLock(NODE, "Phil")
        assert lock.acquire("junior-dev")
        assert state.recover_inflight(NODE) == []
        assert state.queue_depth(NODE, "Phil") == 0
        lock.release()
        assert state.recover_inflight(NODE) == [("phil", 1)]

    def test_idle_recovers_an_orphan_and_says_so(self, ctx, fake_harness):
        queue("Phil")
        state.claim_queue(NODE, "Phil")  # a turn that will never finish
        run_idle(ctx, run_fn=ok_turn)
        assert "RECOVERED phil" in read_log()

    def test_a_sigkill_mid_turn_loses_nothing(self, ctx, tmp_path, r4t_home):
        """The real thing: a turn is killed with SIGKILL between claim and
        completion. Before the claim moved instead of deleting, the batch was
        gone — `.turn.json` records counts and thread ids, not bodies."""
        script = tmp_path / "killer.py"
        script.write_text(
            textwrap.dedent(
                f"""\
                import os, signal, sys
                sys.path.insert(0, {str(Path(state.__file__).parent)!r})
                sys.path.append({str(Path(state.__file__).parent.parent.parent)!r})
                os.environ["R4T_HOME"] = {str(r4t_home)!r}
                import state
                state.claim_queue("{NODE}", "Phil")
                lock = state.AgentLock("{NODE}", "Phil")
                lock.acquire("junior-dev")
                os.kill(os.getpid(), signal.SIGKILL)
                """
            ),
            encoding="utf-8",
        )
        queue("Phil", body="the message that must survive")
        proc = subprocess.run([sys.executable, str(script)])
        assert proc.returncode == -signal.SIGKILL
        assert state.queue_depth(NODE, "Phil") == 0  # claimed, in flight, orphaned
        state.prune_stale_locks(NODE)
        assert state.recover_inflight(NODE) == [("phil", 1)]
        assert state.read_queue(NODE, "Phil")[0]["body"] == (
            "the message that must survive"
        )


class TestPark:
    def _break_junior(self, ctx):
        config = json.loads(ctx.config_path.read_text(encoding="utf-8"))
        config["junior-dev"]["invoke"] = ["nowhere-cli", "{prompt}"]
        ctx.config_path.write_text(json.dumps(config), encoding="utf-8")

    def test_the_first_structural_failure_parks(self, ctx):
        self._break_junior(ctx)
        queue("Phil")
        assert drain(ctx, run_fn=missing_binary_turn) == 1
        parked = state.read_parked(NODE, "Phil")
        assert parked["probe"] == "nowhere-cli"
        assert "No such file" in parked["reason"]

    def test_parking_says_it_once_and_then_goes_quiet(self, ctx):
        self._break_junior(ctx)
        queue("Phil")
        drain(ctx, run_fn=missing_binary_turn)
        assert read_log().count("r4t: PARKED phil") == 1
        for _ in range(3):
            queue("Phil", body="another")
            drain(ctx, run_fn=missing_binary_turn)
        assert read_log().count("r4t: PARKED phil") == 1

    def test_a_parked_member_holds_its_queue_and_leaves_the_rotation(self, ctx):
        self._break_junior(ctx)
        queue("Phil")
        drain(ctx, run_fn=missing_binary_turn)
        queue("Phil", body="more")
        assert state.queue_depth(NODE, "Phil") == 2  # nothing dropped
        entries = look(ctx)
        assert entry_for(entries, "phil").state == schedule.PARKED
        assert schedule.next_up(entries) is None

    def test_the_ticker_carries_one_park_line(self, ctx, capsys):
        self._break_junior(ctx)
        queue("Phil")
        drain(replace(ctx, ticker=True), run_fn=missing_binary_turn)
        lines = [
            ln for ln in capsys.readouterr().out.splitlines()
            if ln.startswith("r4t: PARKED")
        ]
        assert len(lines) == 1
        assert lines[0].split()[2] == "phil"

    def test_a_transient_failure_uses_the_breaker_not_the_park(self, ctx):
        def timed_out(rig, prompt, workdir, *, env=None, variant=0):
            return 1, "the model went away", 30.0, True

        queue("Phil")
        drain(ctx, run_fn=timed_out)
        assert state.read_parked(NODE, "Phil") == {}
        assert state.read_meta(NODE, "Phil")["consecutive_failures"] == 1

    def test_a_free_probe_unparks_on_idle(self, ctx, tmp_path, fake_harness):
        self._break_junior(ctx)
        queue("Phil")
        drain(ctx, run_fn=missing_binary_turn)
        assert state.read_parked(NODE, "Phil")
        binary = tmp_path / "bin" / "nowhere-cli"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        os.environ["PATH"] = f"{binary.parent}{os.pathsep}{os.environ['PATH']}"
        try:
            run_idle(ctx, run_fn=ok_turn)
        finally:
            os.environ["PATH"] = os.environ["PATH"].split(os.pathsep, 1)[1]
        assert state.read_parked(NODE, "Phil") == {}
        assert "RESUME phil" in read_log()

    def test_the_probe_costs_nothing_while_the_cause_stands(self, ctx, fake_harness):
        self._break_junior(ctx)
        queue("Phil")
        drain(ctx, run_fn=missing_binary_turn)
        calls = []

        def counted(rig, prompt, workdir, *, env=None, variant=0):
            calls.append(rig.name)
            return ok_turn(rig, prompt, workdir, env=env, variant=variant)

        run_idle(ctx, run_fn=counted)
        assert "junior-dev" not in calls  # no paid turn to see what happens
        assert state.read_parked(NODE, "Phil")

    def test_resume_returns_a_member_by_hand(self, ctx, capsys):
        self._break_junior(ctx)
        queue("Phil")
        drain(ctx, run_fn=missing_binary_turn)
        assert r4t_main(
            ["resume", "phil", "--node", NODE, "--root", str(ctx.root),
             "--rig-config", str(ctx.config_path)]
        ) == 0
        out = capsys.readouterr().out
        assert "resumed phil" in out and "1 message(s) waiting" in out
        assert state.read_parked(NODE, "Phil") == {}
        assert state.read_meta(NODE, "Phil")["consecutive_failures"] == 0

    def test_resume_all_clears_every_park(self, ctx, capsys):
        self._break_junior(ctx)
        queue("Phil")
        drain(ctx, run_fn=missing_binary_turn)
        assert r4t_main(
            ["resume", "--all", "--node", NODE, "--root", str(ctx.root),
             "--rig-config", str(ctx.config_path)]
        ) == 0
        assert state.parked_members(NODE) == []

    def test_resume_needs_a_target(self, ctx, capsys):
        assert r4t_main(
            ["resume", "--node", NODE, "--root", str(ctx.root),
             "--rig-config", str(ctx.config_path)]
        ) == 2
        assert "name a member, or --all" in capsys.readouterr().err

    def test_resume_rejects_an_unknown_member(self, ctx, capsys):
        assert r4t_main(
            ["resume", "nobody", "--node", NODE, "--root", str(ctx.root),
             "--rig-config", str(ctx.config_path)]
        ) == 2
        assert "no roster member named" in capsys.readouterr().err


class TestStatusRotationBlock:
    def _status(self, ctx, capsys):
        assert r4t_main(
            ["status", "--node", NODE, "--root", str(ctx.root),
             "--rig-config", str(ctx.config_path), "--no-notify"]
        ) == 0
        out = capsys.readouterr().out
        block = out.split("Rotation", 1)[1].split("Health", 1)[0]
        return out, block

    def test_the_rotation_block_comes_before_health(self, ctx, capsys):
        out, _block = self._status(ctx, capsys)
        assert out.index("Rotation") < out.index("Health")

    def test_idle_now_row_names_the_last_turn(self, ctx, capsys, fake_harness):
        handle_message(ctx, "boss", NODE, "hi", run_fn=ok_turn)
        _out, block = self._status(ctx, capsys)
        now = next(ln for ln in block.splitlines() if ln.strip().startswith("Now"))
        assert "idle" in now and "last: gerry" in now and "exit 0" in now

    def test_a_running_turn_is_the_now_row_with_its_timeout(self, ctx, capsys):
        lock = state.AgentLock(NODE, "gerry")
        assert lock.acquire("leader")
        state.write_turn(NODE, "Gerry", {"batch": 3})
        try:
            _out, block = self._status(ctx, capsys)
        finally:
            lock.release()
        now = next(ln for ln in block.splitlines() if ln.strip().startswith("Now"))
        assert "gerry" in now and "running" in now and " of 30s" in now
        assert "3 msg" in now

    def test_next_and_then_carry_the_why_and_the_score(self, ctx, capsys):
        queue("Gerry", origin="ingress")
        queue("Phil")
        state.update_meta(NODE, "Phil", passes=4)
        _out, block = self._status(ctx, capsys)
        rows = {
            ln.split()[0]: ln for ln in
            (line.strip() for line in block.splitlines()) if ln
        }
        assert "phil" in rows["Next"] and "passed over 4" in rows["Next"]
        assert "score 4" in rows["Next"]
        assert "gerry" in rows["Then"] and "ingress" in rows["Then"]
        assert "score 1" in rows["Then"]

    def test_a_priority_row_reads_as_an_override_not_a_score(self, ctx, capsys):
        # No priority sender ships by default — this status line only fires
        # because the org states one.
        (ctx.root / ORG_CONFIG_NAME).write_text(
            json.dumps({"priority_senders": ["neil*"]}), encoding="utf-8"
        )
        queue("Phil", "neil")
        _out, block = self._status(ctx, capsys)
        assert "PRIORITY (neil) — always next" in block

    def test_a_resting_member_is_held_with_its_reason(self, ctx, capsys):
        config = load_rig_config(ctx.config_path)
        rig = config.rig_for(load_roster(ctx.roster_path).find("Phil"))[0]
        state.budget_charge(
            NODE, "Phil", rig.budget_max, rig.budget_earn_per_hour, rig.budget_max + 5
        )
        queue("Phil")
        _out, block = self._status(ctx, capsys)
        held = next(ln for ln in block.splitlines() if "phil" in ln)
        assert "RESTING" in held and "member budget" in held and "1 queued" in held

    def test_an_open_breaker_is_held_with_its_reason(self, ctx, capsys):
        config = load_rig_config(ctx.config_path)
        state.update_meta(
            NODE, "Phil",
            consecutive_failures=config.breaker_cap,
            last_failure_at=state.utc_now(),
        )
        queue("Phil")
        _out, block = self._status(ctx, capsys)
        held = next(ln for ln in block.splitlines() if "phil" in ln)
        assert "BREAKER" in held and "consecutive failed turns" in held

    def test_a_parked_member_is_held_with_its_next_verb(self, ctx, capsys):
        state.park_member(
            NODE, "Phil", reason="failed to spawn harness 'codex'",
            rig="junior-dev", probe="codex",
        )
        queue("Phil")
        _out, block = self._status(ctx, capsys)
        held = next(ln for ln in block.splitlines() if "phil" in ln)
        assert "PARKED" in held and "(try: r4t resume phil)" in held

    def test_quiet_members_are_one_line_not_many(self, ctx, capsys):
        queue("Phil")
        _out, block = self._status(ctx, capsys)
        idle = next(ln for ln in block.splitlines() if ln.strip().startswith("Idle"))
        assert "member(s) with nothing queued" in idle

    def test_the_block_fits_on_a_screen(self, ctx, capsys):
        queue("Gerry")
        queue("Phil")
        _out, block = self._status(ctx, capsys)
        assert len([ln for ln in block.splitlines() if ln.strip()]) <= 12
