"""r4t seat CLI tests — the orchestrator-facing mailbox/voice surface."""
from __future__ import annotations

import json
import os
from pathlib import Path

import state
import tasks
from dispatch import drain, handle_message
from r4t import main as r4t_main

NODE = "acme"


def _seat(repo, rig_config, *args):
    return r4t_main(
        [
            "seat",
            *args,
            "--node",
            NODE,
            "--root",
            str(repo),
            "--rig-config",
            str(rig_config),
            "--simulate-tell",
        ]
    )


def test_seat_summary(repo, rig_config, r4t_home, capsys):
    state.park_seat_message(NODE, "Neil", "acme:gerry", "hello")
    assert _seat(repo, rig_config) == 0
    out = capsys.readouterr().out
    assert "seat: Neil on acme" in out
    assert "unread: 1" in out
    assert "attached: no" in out
    assert "doorbell: neil" in out


def test_seat_inbox_reads_and_marks(repo, rig_config, r4t_home, capsys):
    state.park_seat_message(NODE, "Neil", "acme:gerry", "first")
    state.park_seat_message(NODE, "Neil", "acme:phil", "second")
    assert _seat(repo, rig_config, "inbox") == 0
    out = capsys.readouterr().out
    assert "from acme:gerry" in out and "first" in out
    assert "from acme:phil" in out and "second" in out
    assert state.list_seat_messages(NODE, "neil") == []
    assert len(state.list_seat_messages(NODE, "neil", read=True)) == 2


def test_seat_inbox_peek_and_json(repo, rig_config, r4t_home, capsys):
    state.park_seat_message(NODE, "Neil", "acme:gerry", "hello")
    assert _seat(repo, rig_config, "inbox", "--peek", "--json") == 0
    envelope = json.loads(capsys.readouterr().out.strip())
    assert envelope["from"] == "acme:gerry"
    assert envelope["content"] == "hello"
    assert len(state.list_seat_messages(NODE, "neil")) == 1


def test_park_without_files_records_empty_files(r4t_home):
    path = state.park_seat_message(NODE, "Neil", "acme:gerry", "hello")
    assert json.loads(path.read_text(encoding="utf-8"))["files"] == []


def test_park_with_bundle_copies_into_seat_storage(r4t_home, tmp_path, capsys, repo, rig_config):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "notes.md").write_text("full text", encoding="utf-8")
    path = state.park_seat_message(
        NODE, "Neil", "acme:gerry", "see attached",
        files=[{"filename": "notes.md"}], bundle=bundle,
    )
    (entry,) = json.loads(path.read_text(encoding="utf-8"))["files"]
    stored = Path(entry["path"])
    assert entry["filename"] == "notes.md"
    assert stored.read_text(encoding="utf-8") == "full text"
    assert state.seat_dir(NODE, "neil") in stored.parents
    assert _seat(repo, rig_config, "inbox") == 0
    assert f"(attachment: notes.md at {stored})" in capsys.readouterr().out


def test_seat_send_creates_task_as_human(repo, rig_config, r4t_home, fake_harness):
    assert _seat(repo, rig_config, "send", "--to", "phil", "ship", "it") == 0
    listing = tasks.list_tasks(NODE)
    assert len(listing) == 1
    assert listing[0]["creator"] == "acme:neil"
    _script, out = fake_harness
    assert len(sorted(out.iterdir())) == 1


def test_seat_send_defaults_to_leader(repo, rig_config, r4t_home, fake_harness, capsys):
    assert _seat(repo, rig_config, "send", "hello") == 0
    _script, out = fake_harness
    calls = sorted(out.iterdir())
    assert len(calls) == 1
    assert "You are Gerry" in calls[0].read_text(encoding="utf-8")


def test_seat_send_rejects_unknown_member(repo, rig_config, r4t_home, capsys):
    assert _seat(repo, rig_config, "send", "--to", "nobody", "hi") == 2
    assert "no dispatchable member" in capsys.readouterr().err


def test_live_lock_holds_queue_for_next_drain(ctx, r4t_home, fake_harness):
    # The agent lock is the only claim: a member with a live turn cannot be
    # run by a concurrent drainer, and its queued mail simply waits.
    handle_message(ctx, "acme:gerry", "acme:phil", "go", drain_after=False)
    lock = state.AgentLock(NODE, "phil")
    assert lock.acquire("junior-dev")
    assert drain(ctx) == 0
    assert state.queue_depth(NODE, "phil") == 1
    assert not harness_calls_exist(fake_harness)
    lock.release()
    assert drain(ctx) == 1
    assert harness_calls_exist(fake_harness)


def test_stale_lock_does_not_block_drain(ctx, r4t_home, fake_harness):
    handle_message(ctx, "acme:gerry", "acme:phil", "go", drain_after=False)
    dead = state.agent_dir(NODE, "phil") / ".lock"
    dead.parent.mkdir(parents=True, exist_ok=True)
    dead.write_text(json.dumps({"pid": 999999999, "rig": "junior-dev"}), encoding="utf-8")
    assert drain(ctx) == 1  # stale lock stolen; the queued message runs
    assert harness_calls_exist(fake_harness)


def harness_calls_exist(fake_harness) -> bool:
    _script, out = fake_harness
    return bool(sorted(out.iterdir()))
