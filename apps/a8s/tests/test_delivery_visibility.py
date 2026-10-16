"""What an operator can read about a message on the machine that handled it.

Every outcome is a transaction row, read with `a8s tx --from NAME` or
`a8s trace <ULID>`. a8s keeps no per-message ledger for the sender: the agent
knows only that it sent, and nothing about a failure crosses the network.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import txlog
from ar3.ulid import new as new_ulid
from delivery_receipt import build_delivery_receipt, parse_delivery_receipt


def _stamp(delta_s: float = 0.0) -> str:
    when = datetime.now(timezone.utc) + timedelta(seconds=delta_s)
    return when.isoformat().replace("+00:00", "Z")


def _envelope(msg_id: str, to: str = "bob", files: list[dict] | None = None) -> dict:
    return {
        "id": msg_id,
        "date": _stamp(),
        "from": "alice",
        "to": to,
        "content": "hello",
        "files": files or [],
    }


def _events(msg_id: str) -> list[str]:
    return [r["event"] for r in txlog.read_events(msg_id)]


def test_new_events_are_registered():
    for event in ("ENQUEUED", "ATTACHMENT_FAILED", "DELIVERY_RECEIPT"):
        assert event in txlog.EVENTS
    assert "NO_LOCAL_RECIPIENT" not in txlog.EVENTS


@pytest.mark.parametrize("stage", ["inbox_write", "attachment_failed"])
def test_receipt_v2_round_trips_every_stage(stage):
    envelope = build_delivery_receipt(
        _envelope(new_ulid()), ["bob"], stage,
        files=["notes.md"], detail="storage unreadable",
    )
    parsed = parse_delivery_receipt(envelope)
    assert parsed is not None
    assert parsed.stage == stage
    assert parsed.files == ("notes.md",)
    assert parsed.detail == "storage unreadable"


@pytest.mark.parametrize(
    "stage", ["invented", "attachment_fetched", "deferred", "expired"],
)
def test_receipt_rejects_a_stage_it_does_not_send(stage):
    assert build_delivery_receipt(_envelope(new_ulid()), ["bob"], stage) is None


def _two_agents(tmp_path: Path):
    from core import Participant
    from registry import save_registry

    a_root, b_root = tmp_path / "A", tmp_path / "B"
    a_root.mkdir()
    b_root.mkdir()
    save_registry({"A": {"root": str(a_root)}, "B": {"root": str(b_root)}})
    return a_root, [Participant("A", a_root), Participant("B", b_root)]


def test_an_arriving_receipt_is_a_row_and_writes_no_file(fake_home, tmp_path: Path):
    """The receive path logs each receipt in the transaction log. It writes
    nothing into the sender's outbox."""
    from core import outbox_dir
    from network import receive_envelope

    a_root, agents = _two_agents(tmp_path)
    msg_id = new_ulid()
    original = {"id": msg_id, "from": "A", "to": "B"}
    for stage, files, detail in (
        ("attachment_failed", ["notes.md"], "storage unreadable"),
        ("inbox_write", [], ""),
    ):
        envelope = build_delivery_receipt(original, ["B"], stage, files=files, detail=detail)
        receive_envelope(json.dumps(envelope).encode(), agents, remote_id="cluster")

    rows = txlog.read_events(msg_id)
    assert [r["event"] for r in rows] == [
        "DELIVERY_RECEIPT", "ATTACHMENT_FAILED", "DELIVERY_RECEIPT",
    ]
    failed = [r for r in rows if r["event"] == "ATTACHMENT_FAILED"][0]
    assert failed["detail"] == "storage unreadable"
    assert failed["files"] == "notes.md"
    assert not outbox_dir(a_root).exists() or list(outbox_dir(a_root).iterdir()) == []


def test_a_message_for_nobody_here_sends_nothing_back(fake_home, tmp_path: Path):
    """a8s is best effort: a node that owns none of a message's recipients
    logs the miss locally and sends the sender nothing. It records the id, so
    a second daemon on the same machine logs nothing more."""
    import network
    from core import Participant
    from network import receive_envelope
    from registry import save_registry

    b_root = tmp_path / "B"
    b_root.mkdir()
    save_registry({"B": {"root": str(b_root)}})
    published: list[bytes] = []
    msg = dict(_envelope(new_ulid(), to="nobody"), **{"from": "A"})
    raw = json.dumps(msg).encode()
    for _daemon in range(2):
        network._REMOTE_DIAGNOSTIC_LAST.clear()
        receive_envelope(
            raw, [Participant("B", b_root)],
            publish_control=published.append, remote_id="cluster",
        )
    assert published == []
    assert network.seen_id_contains(msg["id"])
    assert _events(msg["id"]) == ["NOT_LOCAL"]


@pytest.mark.parametrize(
    "stage", ["no_local_recipient", "attachment_fetched", "deferred", "expired"],
)
def test_an_older_node_s_receipt_stage_drops_quietly(fake_home, tmp_path: Path, stage):
    """A node on an older release still sends stages this release does not.
    The receiving node drops that receipt and writes no row at all, not even a
    DISCARDED one."""
    from core import agent_log_path
    from network import receive_envelope

    _, agents = _two_agents(tmp_path)
    msg_id = new_ulid()
    old = build_delivery_receipt({"id": msg_id, "from": "A", "to": "B"}, ["B"], "inbox_write")
    old["a8s_control"]["stage"] = stage
    assert parse_delivery_receipt(old) is None
    receive_envelope(json.dumps(old).encode(), agents, remote_id="cluster")
    assert txlog.read_recent(limit=50, local_only=False) == []
    log = agent_log_path("A")
    assert not log.exists() or msg_id not in log.read_text()


def test_a_malformed_receipt_is_still_discarded(fake_home, tmp_path: Path):
    """Positive control for the quiet drop: a receipt of an unknown version
    is malformed, and that leaves a DISCARDED row."""
    from network import receive_envelope

    _, agents = _two_agents(tmp_path)
    bad = build_delivery_receipt({"id": new_ulid(), "from": "A", "to": "B"}, ["B"], "inbox_write")
    bad["a8s_control"]["version"] = 99
    receive_envelope(json.dumps(bad).encode(), agents, remote_id="cluster")
    assert _events(bad["id"]) == ["DISCARDED"]


def test_a_32_hour_delivery_is_marked_late_and_a_fresh_one_is_not():
    from tells import late_prefix

    queued = datetime.now(timezone.utc) - timedelta(hours=32)
    late = {
        "date": queued.isoformat().replace("+00:00", "Z"),
        "delivered_at": _stamp(),
    }
    assert late_prefix(late) == "[late 32h] "
    prompt = {"date": _stamp(-60), "delivered_at": _stamp()}
    assert late_prefix(prompt) == ""
    assert late_prefix({"date": _stamp()}) == ""


def _sender(tmp_path: Path, *others: str):
    from core import Participant
    from mailbox import ensure_mailboxes
    from registry import save_registry

    roots = {name: tmp_path / name.lower() for name in ("A", *others)}
    for root in roots.values():
        root.mkdir()
    save_registry({name: {"root": str(root)} for name, root in roots.items()})
    agents = [Participant(name, root) for name, root in roots.items()]
    for agent in agents:
        ensure_mailboxes(agent)
    return roots["A"], agents


def test_a_local_send_reads_back_as_rows_from_the_sender(fake_home, tmp_path: Path, capsys):
    """The operator's view end to end: route once, then `a8s tx --from A`."""
    from commands import cmd_transactions
    from core import outbox_dir
    from mailbox import _write_outbox, route_outboxes

    a_root, agents = _sender(tmp_path, "B")
    _write_outbox("A", a_root, "B", "hi", [])
    assert route_outboxes(agents, all_agents=agents) == 1

    assert list(outbox_dir(a_root).iterdir()) == []
    assert cmd_transactions(["--from", "A"]) == 0
    out = capsys.readouterr().out
    assert "ENQUEUED" in out
    assert "ROUTED" in out


def test_a_send_that_no_hop_took_is_enqueued_then_dropped(fake_home, tmp_path: Path):
    from mailbox import _write_outbox, route_outboxes

    a_root, agents = _sender(tmp_path)
    msg_id = _write_outbox("A", a_root, "faraway", "are you there", []).stem
    route_outboxes(
        agents,
        all_agents=agents,
        publish_remotes=lambda *a, **k: {"cluster": ("failed", "cluster: down")},
        configured_remote_ids=["cluster"],
    )
    events = _events(msg_id)
    assert events[0] == "ENQUEUED"
    assert "DROPPED" in events or "DISCARDED" in events


def test_an_unknown_recipient_with_no_remote_is_enqueued_then_dropped(
    fake_home, tmp_path: Path
):
    from mailbox import _write_outbox, route_outboxes

    a_root, agents = _sender(tmp_path)
    msg_id = _write_outbox("A", a_root, "ghost", "anyone there", []).stem
    route_outboxes(agents, all_agents=agents)
    assert _events(msg_id) == ["ENQUEUED", "DROPPED"]


def test_enqueued_is_logged_once_when_a_pass_is_cut_short(fake_home, tmp_path: Path):
    """Ingest moves the envelope out of the outbox, so it logs ENQUEUED once.
    A pass that stops before routing leaves the file in pending, and the next
    pass routes it without a second ENQUEUED."""
    from mailbox import _ingest_outboxes, _write_outbox, route_outboxes

    a_root, agents = _sender(tmp_path, "B")
    msg_id = _write_outbox("A", a_root, "B", "hi", []).stem
    _ingest_outboxes(agents, agents)
    assert _events(msg_id) == ["ENQUEUED"]
    route_outboxes(agents, all_agents=agents)
    assert _events(msg_id) == ["ENQUEUED", "ROUTED"]
