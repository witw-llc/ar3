"""`r4t task trace` — the delegation tree reconstructed from recorded state.

Every fixture here is real dispatch output: the trace is only worth anything if
it reads the files dispatch actually writes, so no test hand-writes a log line.
"""
from __future__ import annotations

import json

import pytest

import dispatch
import state
import tasks
import tasktrace
from dispatch import drain_until_quiet, handle_message
from r4t import main as r4t_main
from rig import load_rig_config
from roster import load_roster
from ulid import new as new_ulid

from test_dispatch import dead_reasons, run_one

NODE = "acme"


def only_thread() -> str:
    listing = tasks.list_tasks(NODE)
    assert len(listing) == 1, [t["id"] for t in listing]
    return str(listing[0]["id"])


def shape(trace) -> list[tuple[int, str, str, int]]:
    return [
        (depth, edge.sender, edge.to, edge.hop)
        for depth, edge in tasktrace.delegation(trace.edges)
    ]


@pytest.fixture
def two_hop(chatty_ctx, chatty_harness, monkeypatch):
    """boss asks the roster; Gerry delegates to Phil; Phil answers the human
    seat. Three deliveries, two turns, one thread."""
    monkeypatch.setenv("CHATTY_TO", "acme:phil")
    monkeypatch.setenv("CHATTY_BODY", "take the tokenizer")
    assert run_one(chatty_ctx, "boss", "acme", "ship the parser") == 1
    monkeypatch.setenv("CHATTY_TO", "neil")
    monkeypatch.setenv("CHATTY_BODY", "tokenizer landed")
    assert drain_until_quiet(chatty_ctx) == 1
    return chatty_ctx


class TestDelegationTree:
    def test_reconstructs_sender_recipient_hops(self, two_hop):
        trace = tasktrace.build(NODE, only_thread())
        assert shape(trace) == [
            (0, "boss", "gerry", 0),
            (1, "gerry", "phil", 1),
            (2, "phil", "neil", 2),
        ]

    def test_carries_the_queued_preview(self, two_hop):
        trace = tasktrace.build(NODE, only_thread())
        previews = {edge.to: edge.preview for edge in trace.edges}
        assert previews["gerry"] == "ship the parser"
        assert previews["phil"] == "take the tokenizer"

    def test_records_every_turn_with_outcome(self, two_hop):
        trace = tasktrace.build(NODE, only_thread())
        assert [(t.member, t.rig, t.exit_code) for t in trace.turns] == [
            ("gerry", "leader", 0),
            ("phil", "junior-dev", 0),
        ]
        assert all(t.duration is not None and not t.killed for t in trace.turns)

    def test_seat_delivery_has_no_duplicate_edge(self, two_hop):
        # A human recipient parks in the seat: only the sender's
        # RELEASED-internal is logged, never a QUEUED. One edge either way.
        trace = tasktrace.build(NODE, only_thread())
        assert [e for e in trace.edges if e.to == "neil"][0].preview == ""
        assert len(trace.edges) == 3

    def test_two_seat_sends_at_one_hop_stay_two_edges(
        self, chatty_ctx, chatty_harness, monkeypatch
    ):
        monkeypatch.setenv("CHATTY_TO", "neil")
        monkeypatch.setenv("CHATTY_SENDS", "2")
        assert run_one(chatty_ctx, "acme:gerry", "acme:phil", "ship it") == 1
        trace = tasktrace.build(NODE, only_thread())
        assert shape(trace) == [
            (0, "gerry", "phil", 0),
            (1, "phil", "neil", 1),
            (1, "phil", "neil", 1),
        ]

    def test_open_thread_reads_open(self, two_hop):
        trace = tasktrace.build(NODE, only_thread())
        assert not trace.closed
        assert trace.answered_by is None
        assert trace.ledger["creator"] == "boss"


class TestClosure:
    def test_answer_to_the_originator_marks_the_edge(
        self, chatty_ctx, chatty_harness, monkeypatch
    ):
        monkeypatch.setenv("CHATTY_TO", "boss-agent")
        monkeypatch.setenv("CHATTY_BODY", "done: shipped and verified")
        assert run_one(chatty_ctx, "boss-agent", "acme:phil", "ship it") == 1
        trace = tasktrace.build(NODE, only_thread())
        assert trace.closed
        # External mail always enters at the top, so Gerry answers it.
        assert trace.answered_by == "gerry -> boss-agent"
        egress = [e for e in trace.edges if e.to == "boss-agent"]
        assert len(egress) == 1
        assert egress[0].external and egress[0].closes
        assert "out of the walls" in "\n".join(tasktrace.render(trace))


class TestMissingLedger:
    def test_expired_ledger_still_traces_from_the_log(self, two_hop):
        thread = only_thread()
        assert tasks.expire_tasks(NODE, older_than_seconds=0) == [thread]
        trace = tasktrace.build(NODE, thread)
        assert trace.ledger is None
        assert not trace.empty
        assert shape(trace)[0] == (0, "boss", "gerry", 0)
        text = "\n".join(tasktrace.render(trace))
        assert "expired" in text
        assert "originator  boss" in text

    def test_expired_and_closed_reads_closed_from_the_log(
        self, chatty_ctx, chatty_harness, monkeypatch
    ):
        monkeypatch.setenv("CHATTY_TO", "boss-agent")
        monkeypatch.setenv("CHATTY_BODY", "done: shipped and verified")
        run_one(chatty_ctx, "boss-agent", "acme:phil", "ship it")
        thread = only_thread()
        tasks.expire_tasks(NODE, older_than_seconds=0)
        trace = tasktrace.build(NODE, thread)
        assert trace.ledger is None and trace.closed


class TestMidFlight:
    def test_queued_but_not_yet_run(self, ctx, fake_harness):
        handle_message(ctx, "boss", "acme", "ship the parser", drain_after=False)
        trace = tasktrace.build(NODE, only_thread())
        assert trace.turns == []
        assert shape(trace) == [(0, "boss", "gerry", 0)]
        assert [(q["member"], q["hop"]) for q in trace.queued] == [("gerry", 0)]
        assert "In flight" in "\n".join(tasktrace.render(trace))

    def test_running_turn_without_a_live_lock_reads_crashed(self, ctx):
        handle_message(ctx, "boss", "acme", "ship the parser", drain_after=False)
        thread = only_thread()
        state.write_turn(
            NODE, "gerry",
            {"threads": [thread], "started": "2026-07-29T00:00:00Z", "rig": "leader"},
        )
        trace = tasktrace.build(NODE, thread)
        assert [(r["member"], r["live"]) for r in trace.running] == [("gerry", False)]
        assert "crashed" in "\n".join(tasktrace.render(trace))


class TestTrouble:
    def test_dead_letters_land_in_the_trace(
        self, chatty_ctx, chatty_harness, monkeypatch
    ):
        monkeypatch.setenv("CHATTY_TO", "gerry")
        monkeypatch.setenv("CHATTY_SENDS", "4")  # max_sends_per_turn is 2
        run_one(chatty_ctx, "acme:gerry", "acme:phil", "fan out")
        assert dead_reasons() == ["quota", "quota"]
        trace = tasktrace.build(NODE, only_thread())
        assert [d["reason"] for d in trace.dead_letters] == ["quota", "quota"]
        assert "Dead letters" in "\n".join(tasktrace.render(trace))

    def test_thread_tagged_events_land_in_the_trace(self, ctx):
        task = tasks.new_task(new_ulid(), "acme:neil")
        task["updated_at"] = "2020-01-01T00:00:00Z"
        state.atomic_write_json(tasks.task_path(NODE, task["id"]), task)
        roster = load_roster(ctx.roster_path)
        config = load_rig_config(ctx.config_path)
        assert dispatch._quiet_task_sweep(ctx, config, roster) == [task["id"]]
        trace = tasktrace.build(NODE, task["id"])
        assert [e.split()[0] for e in trace.events] == ["QUIET"]
        assert "Events" in "\n".join(tasktrace.render(trace))

    def test_a_failed_turn_points_at_the_captured_output(
        self, ctx, repo, rig_config, monkeypatch
    ):
        script = repo / "boom.py"
        script.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
        config = json.loads(rig_config.read_text(encoding="utf-8"))
        config["leader"]["invoke"] = ["python3", str(script), "{prompt}"]
        rig_config.write_text(json.dumps(config), encoding="utf-8")
        run_one(ctx, "boss", "acme", "ship the parser")
        trace = tasktrace.build(NODE, only_thread())
        assert [t.exit_code for t in trace.turns] == [3]
        text = "\n".join(tasktrace.render(trace))
        assert "r4t logs --node acme --agent gerry --full" in text


class TestCli:
    def test_trace_prints_the_panel(self, two_hop, capsys):
        assert r4t_main(["task", "trace", only_thread(), "--node", NODE]) == 0
        out = capsys.readouterr().out
        for section in ("Thread", "Delegation", "Turns"):
            assert section in out
        assert "boss -> gerry" in out
        assert "gerry -> phil" in out

    def test_trace_json_is_machine_readable(self, two_hop, capsys):
        thread = only_thread()
        assert r4t_main(["task", "trace", thread, "--node", NODE, "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["thread"] == thread
        assert data["originator"] == "boss"
        assert [(e["from"], e["to"], e["hop"]) for e in data["delegation"]] == [
            ("boss", "gerry", 0),
            ("gerry", "phil", 1),
            ("phil", "neil", 2),
        ]
        assert [t["member"] for t in data["turns"]] == ["gerry", "phil"]

    def test_trace_lowercase_id_resolves(self, two_hop, capsys):
        assert r4t_main(["task", "trace", only_thread().lower(), "--node", NODE]) == 0
        assert "Delegation" in capsys.readouterr().out

    def test_unknown_thread_says_nothing_recorded(self, two_hop, capsys):
        assert r4t_main(["task", "trace", new_ulid(), "--node", NODE]) == 1
        err = capsys.readouterr().err
        assert "nothing recorded" in err
        assert "(try: r4t task list --node acme)" in err

    def test_trace_without_an_id_is_a_usage_error(self, two_hop, capsys):
        assert r4t_main(["task", "trace", "--node", NODE]) == 2
        assert "task trace: <id> is required" in capsys.readouterr().err
