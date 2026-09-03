"""What the SENDER can read about a message it sent (#93).

Each test asserts a sender-visible surface — the receipt file, the
`tells --sent` line — rather than the internal helper that produced it. One
control per failure mode in the design.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import receipts
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


def test_new_events_are_registered():
    for event in ("ENQUEUED", "DEFERRED", "ATTACHMENT_FAILED", "EXPIRED",
                  "NO_LOCAL_RECIPIENT"):
        assert event in txlog.EVENTS


@pytest.mark.parametrize(
    "stage",
    ["inbox_write", "attachment_fetched", "attachment_failed", "deferred",
     "expired", "no_local_recipient"],
)
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


def test_receipt_rejects_an_unknown_stage():
    assert build_delivery_receipt(_envelope(new_ulid()), ["bob"], "invented") is None


def test_unreadable_storage_reaches_the_sender_receipt(tmp_path: Path):
    """Failure mode 1: bytes parked where no receiver can read them."""
    outbox = tmp_path / ".outbox"
    msg_id = new_ulid()
    receipts.record_enqueued(outbox, _envelope(msg_id))
    receipts.record_event(
        outbox, msg_id, "inbox_write", recipient="bob",
    )
    receipts.record_event(
        outbox, msg_id, "attachment_failed", recipient="bob",
        files=["notes.md"], detail="could not download after 900s",
    )
    record = receipts.read(outbox, msg_id)
    assert record["state"] == receipts.STATE_FAILED
    assert record["recipients"]["bob"]["files"]["notes.md"]["state"] == "attachment_failed"
    line = receipts.summary_line(record)
    assert "failed" in line and "could not download" in line
    assert receipts.is_terminal(record)


@pytest.mark.parametrize(
    "order",
    [
        ("attachment_failed", "inbox_write"),
        ("inbox_write", "attachment_failed"),
    ],
    ids=["real_order_attachment_failed_then_inbox_write", "reverse_order"],
)
def test_a_failed_attachment_stays_failed_regardless_of_publish_order(
    tmp_path: Path, order
):
    """The router publishes `attachment_failed`/`attachment_fetched` from
    `_report_attachment_outcome` and `inbox_write` from a separate,
    later `_publish_delivery_receipt` call (network.py ~847-870) -- two
    independent control envelopes over a transport that does not guarantee
    order. Whichever lands first, an attachment_failed recorded against a
    recipient's files must not be erased by an inbox_write that lands
    beside it: `recipient_state` may truthfully read "inbox_write" (the
    body did arrive), but the roll-up and the summary must still say
    failed."""
    outbox = tmp_path / ".outbox"
    msg_id = new_ulid()
    receipts.record_enqueued(outbox, _envelope(msg_id))
    for stage in order:
        if stage == "attachment_failed":
            receipts.record_event(
                outbox, msg_id, "attachment_failed", recipient="bob",
                files=["notes.md"], detail="could not download after 900s",
            )
        else:
            receipts.record_event(outbox, msg_id, "inbox_write", recipient="bob")
    record = receipts.read(outbox, msg_id)
    assert record["state"] == receipts.STATE_FAILED
    assert receipts.is_terminal(record)
    assert record["recipients"]["bob"]["files"]["notes.md"]["state"] == "attachment_failed"
    line = receipts.summary_line(record)
    assert "failed" in line and "could not download" in line
    assert "delivered" not in line


def test_the_real_publish_order_does_not_hide_an_attachment_failure(
    fake_home, tmp_path: Path
):
    """Same finding, reproduced through the actual receive path
    (`network.receive_envelope`) rather than calling `receipts.record_event`
    directly, in the literal order the router publishes: attachment_failed
    before inbox_write."""
    from core import Participant, outbox_dir
    from network import receive_envelope
    from registry import save_registry

    a_root, b_root = tmp_path / "A", tmp_path / "B"
    a_root.mkdir()
    b_root.mkdir()
    save_registry({"A": {"root": str(a_root)}, "B": {"root": str(b_root)}})
    agents = [Participant("A", a_root), Participant("B", b_root)]

    msg_id = new_ulid()
    original = {"id": msg_id, "from": "A", "to": "B"}
    for stage, files, detail in (
        ("attachment_failed", ["notes.md"], "storage unreadable"),
        ("inbox_write", [], ""),
    ):
        envelope = build_delivery_receipt(original, ["B"], stage, files=files, detail=detail)
        receive_envelope(json.dumps(envelope).encode(), agents, remote_id="cluster")

    record = receipts.read(outbox_dir(a_root), msg_id)
    assert record is not None
    assert record["state"] == receipts.STATE_FAILED
    assert receipts.is_terminal(record)
    assert record["recipients"]["B"]["files"]["notes.md"]["detail"] == "storage unreadable"
    assert "failed" in receipts.summary_line(record)


def test_tells_sent_shows_failed_when_attachment_failure_precedes_inbox_write(
    tmp_path: Path, monkeypatch, capsys
):
    """The sender-visible surface, not just the internal helper: `tells
    --sent` must print this message as failed, never delivered."""
    import tells

    outbox = tmp_path / ".outbox"
    msg_id = new_ulid()
    receipts.record_enqueued(outbox, _envelope(msg_id))
    receipts.record_event(
        outbox, msg_id, "attachment_failed", recipient="bob",
        files=["notes.md"], detail="storage unreadable",
    )
    receipts.record_event(outbox, msg_id, "inbox_write", recipient="bob")
    monkeypatch.setattr(tells, "find_outbox", lambda: outbox)
    assert tells._print_sent(None) == 0
    out = capsys.readouterr().out
    assert msg_id in out
    assert "failed" in out
    assert "delivered" not in out


def test_one_failing_and_one_succeeding_recipient_are_both_visible(tmp_path: Path):
    outbox = tmp_path / ".outbox"
    msg_id = new_ulid()
    receipts.record_enqueued(outbox, _envelope(msg_id, to="team"))
    receipts.record_event(outbox, msg_id, "inbox_write", recipient="bob")
    receipts.record_event(
        outbox, msg_id, "attachment_fetched", recipient="bob", files=["notes.md"],
    )
    receipts.record_event(outbox, msg_id, "inbox_write", recipient="carol")
    receipts.record_event(
        outbox, msg_id, "attachment_failed", recipient="carol",
        files=["notes.md"], detail="storage unreadable",
    )
    record = receipts.read(outbox, msg_id)
    assert record["recipients"]["bob"]["files"]["notes.md"]["state"] == "attachment_fetched"
    assert record["recipients"]["carol"]["files"]["notes.md"]["state"] == "attachment_failed"
    assert record["state"] == receipts.STATE_FAILED


def test_published_with_no_receipt_says_so_and_is_not_delivered(tmp_path: Path):
    """Failure mode 2: the receiver is down; nothing may promote a publish."""
    outbox = tmp_path / ".outbox"
    msg_id = new_ulid()
    receipts.record_enqueued(outbox, _envelope(msg_id))
    receipts.record_published(outbox, msg_id, "bob", "cluster")
    record = receipts.read(outbox, msg_id)
    assert record["state"] == receipts.STATE_PUBLISHED
    assert not receipts.is_terminal(record)
    assert "no receipt" in receipts.summary_line(record)


def test_deferred_is_a_state_the_sender_can_see(tmp_path: Path):
    """Failure mode 3: custody without delivery is no longer in-memory only."""
    outbox = tmp_path / ".outbox"
    msg_id = new_ulid()
    receipts.record_enqueued(outbox, _envelope(msg_id))
    receipts.record_event(
        outbox, msg_id, "deferred", recipient="bob",
        files=["notes.md"], detail="attachment download deferred",
    )
    record = receipts.read(outbox, msg_id)
    assert record["state"] == receipts.STATE_DEFERRED
    assert not receipts.is_terminal(record)
    receipts.record_event(
        outbox, msg_id, "expired", recipient="bob", detail="retry window exhausted",
    )
    record = receipts.read(outbox, msg_id)
    assert record["state"] == receipts.STATE_FAILED
    assert receipts.is_terminal(record)


def test_unknown_recipient_on_a_remote_network_publishes_a_receipt():
    """Failure mode 4: with a remote configured the publish succeeds and the
    message evaporates. The receiving node has to say it owns nobody."""
    import network

    published: list[bytes] = []
    msg = _envelope(new_ulid(), to="nobody")
    network._remote_not_local(
        msg["id"], "nobody", "not in local registry", "cluster",
        original=msg, publish_control=published.append,
    )
    assert published, "a node that owns no recipient must say so"
    parsed = parse_delivery_receipt(json.loads(published[0].decode("utf-8")))
    assert parsed is not None
    assert parsed.stage == "no_local_recipient"
    assert parsed.for_id == msg["id"]
    assert parsed.detail == "not in local registry"


def test_no_local_recipient_leaves_the_sender_unconfirmed(tmp_path: Path):
    outbox = tmp_path / ".outbox"
    msg_id = new_ulid()
    receipts.record_enqueued(outbox, _envelope(msg_id, to="nobody"))
    receipts.record_event(
        outbox, msg_id, "no_local_recipient", recipient="nobody",
        detail="not in local registry",
    )
    record = receipts.read(outbox, msg_id)
    # Never delivered, never yet failed: on a shared topic every node that
    # owns nobody reports one of these, so it is evidence, not a verdict.
    assert record["state"] == receipts.STATE_UNCONFIRMED
    assert not receipts.is_terminal(record)
    assert record["unclaimed"][0]["detail"] == "not in local registry"
    assert "no node claimed it" in receipts.summary_line(record)


def test_a_node_owning_nobody_cannot_un_deliver_a_delivered_message(tmp_path: Path):
    outbox = tmp_path / ".outbox"
    msg_id = new_ulid()
    receipts.record_enqueued(outbox, _envelope(msg_id))
    receipts.record_event(outbox, msg_id, "inbox_write", recipient="bob")
    receipts.record_event(
        outbox, msg_id, "no_local_recipient", recipient="bob",
        detail="not in local registry",
    )
    record = receipts.read(outbox, msg_id)
    assert record["state"] == receipts.STATE_DELIVERED
    assert receipts.is_terminal(record)


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


def test_tells_sent_lists_the_seat_s_own_ulids(tmp_path: Path, monkeypatch, capsys):
    import tells

    outbox = tmp_path / ".outbox"
    first, second = sorted([new_ulid(), new_ulid()])
    receipts.record_enqueued(outbox, _envelope(first))
    receipts.record_event(outbox, first, "inbox_write", recipient="bob")
    receipts.record_enqueued(outbox, _envelope(second, to="carol"))
    receipts.record_event(
        outbox, second, "attachment_failed", recipient="carol",
        files=["notes.md"], detail="storage unreadable",
    )
    monkeypatch.setattr(tells, "find_outbox", lambda: outbox)
    assert tells._print_sent(None) == 0
    out = capsys.readouterr().out
    assert first in out and "delivered" in out
    assert second in out and "failed" in out


def test_since_filters_older_receipts(tmp_path: Path):
    outbox = tmp_path / ".outbox"
    old_id, new_id = new_ulid(), new_ulid()
    old = _envelope(old_id)
    old["date"] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat().replace(
        "+00:00", "Z"
    )
    receipts.record_enqueued(outbox, old)
    receipts.record_enqueued(outbox, _envelope(new_id))
    recent = [r["id"] for r in receipts.list_recent(outbox, receipts.parse_duration("2h"))]
    assert new_id in recent and old_id not in recent


def test_a_real_local_send_gets_its_receipt_from_the_router(fake_home, tmp_path: Path):
    """The contract end to end: nothing but `route_outboxes` runs, and the
    sender can read the outcome afterwards. A sender process writes no receipt
    of its own — `tell` only ever drops an envelope in the outbox."""
    from core import Participant, outbox_dir
    from mailbox import _write_outbox, ensure_mailboxes, route_outboxes
    from registry import save_registry

    a_root, b_root = tmp_path / "a", tmp_path / "b"
    a_root.mkdir()
    b_root.mkdir()
    save_registry({"A": {"root": str(a_root)}, "B": {"root": str(b_root)}})
    alice, bob = Participant("A", a_root), Participant("B", b_root)
    ensure_mailboxes(alice)
    ensure_mailboxes(bob)

    _write_outbox("A", a_root, "B", "hi", [])
    assert route_outboxes([alice, bob], all_agents=[alice, bob]) == 1

    sent = receipts.list_recent(outbox_dir(a_root))
    assert len(sent) == 1
    assert sent[0]["state"] == receipts.STATE_DELIVERED
    assert sent[0]["recipients"]["B"]["state"] == "inbox_write"
    assert receipts.is_terminal(sent[0])


def test_the_router_opens_the_receipt_before_anyone_delivers(fake_home, tmp_path: Path):
    """A message parked in pending — its recipient is remote and the remote is
    unreachable — still has a receipt, because the router opens one the first
    time it sees the envelope. Without that, a send that never gets anywhere
    is a send with no record at all, which is the original complaint."""
    from core import Participant, outbox_dir
    from mailbox import _write_outbox, ensure_mailboxes, route_outboxes
    from registry import save_registry

    a_root = tmp_path / "a"
    a_root.mkdir()
    save_registry({"A": {"root": str(a_root)}})
    alice = Participant("A", a_root)
    ensure_mailboxes(alice)

    _write_outbox("A", a_root, "faraway", "are you there", [])
    route_outboxes(
        [alice],
        all_agents=[alice],
        publish_remotes=lambda *a, **k: [],
        configured_remote_ids=["cluster"],
    )

    sent = receipts.list_recent(outbox_dir(a_root))
    assert len(sent) == 1
    assert sent[0]["state"] == receipts.STATE_ENQUEUED
    assert sent[0]["to"] == "faraway"


def test_an_arriving_receipt_lands_in_the_sender_s_outbox(fake_home, tmp_path: Path):
    """The sender's node writes the receipt file when the confirmation comes
    back over the transport. `tell` and `tells` never write one."""
    from core import Participant, outbox_dir
    from network import receive_envelope
    from registry import save_registry

    a_root, b_root = tmp_path / "A", tmp_path / "B"
    a_root.mkdir()
    b_root.mkdir()
    save_registry({"A": {"root": str(a_root)}, "B": {"root": str(b_root)}})
    agents = [Participant("A", a_root), Participant("B", b_root)]

    msg_id = new_ulid()
    original = {"id": msg_id, "from": "A", "to": "B"}
    for stage, files, detail in (
        ("inbox_write", [], ""),
        ("attachment_failed", ["notes.md"], "storage unreadable"),
    ):
        envelope = build_delivery_receipt(original, ["B"], stage, files=files, detail=detail)
        receive_envelope(json.dumps(envelope).encode(), agents, remote_id="cluster")

    record = receipts.read(outbox_dir(a_root), msg_id)
    assert record is not None
    assert record["state"] == receipts.STATE_FAILED
    assert record["recipients"]["B"]["files"]["notes.md"]["detail"] == "storage unreadable"


def test_an_unknown_recipient_with_no_remote_is_visible_to_the_sender(
    fake_home, tmp_path: Path
):
    from core import Participant, outbox_dir
    from mailbox import _write_outbox, ensure_mailboxes, route_outboxes
    from registry import save_registry

    a_root = tmp_path / "a"
    a_root.mkdir()
    save_registry({"A": {"root": str(a_root)}})
    alice = Participant("A", a_root)
    ensure_mailboxes(alice)

    _write_outbox("A", a_root, "ghost", "anyone there", [])
    route_outboxes([alice], all_agents=[alice])

    sent = receipts.list_recent(outbox_dir(a_root))
    assert len(sent) == 1
    assert sent[0]["state"] == receipts.STATE_UNCONFIRMED
    assert sent[0]["unclaimed"][0]["name"] == "ghost"


def test_receipts_live_in_the_outbox_the_router_owns(tmp_path: Path):
    outbox = tmp_path / ".outbox"
    msg_id = new_ulid()
    receipts.record_enqueued(outbox, _envelope(msg_id))
    assert (outbox / ".receipts" / f"{msg_id}.json").is_file()
