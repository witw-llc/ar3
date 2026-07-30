from __future__ import annotations

from delivery_receipt import (
    CONTROL_FIELD,
    RECEIPT_TARGET,
    build_delivery_receipt,
    is_control_envelope,
    parse_delivery_receipt,
)
from ulid import new as new_ulid


def test_build_receipt_is_extension_only_and_contains_no_message_content():
    original_id = new_ulid()
    receipt = build_delivery_receipt({
        "id": original_id,
        "from": "alice",
        "to": "bob",
        "content": "private message",
        "files": [],
    }, ["bob"])

    assert receipt is not None
    assert receipt["to"] == RECEIPT_TARGET
    assert receipt["content"] == ""
    assert receipt["files"] == []
    assert receipt[CONTROL_FIELD] == {
        "type": "delivery_receipt",
        "version": 1,
        "for_id": original_id,
        "sender": "alice",
        "recipients": ["bob"],
        "stage": "inbox_write",
    }
    assert "private message" not in repr(receipt)


def test_parse_receipt_round_trip_and_deduplicates_recipients():
    receipt = build_delivery_receipt(
        {"id": new_ulid(), "from": "alice"},
        ["bob", "bob", "carol"],
    )
    parsed = parse_delivery_receipt(receipt)
    assert parsed is not None
    assert parsed.recipients == ("bob", "carol")
    assert is_control_envelope(receipt)


def test_unknown_control_version_is_not_interpreted_as_receipt():
    receipt = build_delivery_receipt(
        {"id": new_ulid(), "from": "alice"},
        ["bob"],
    )
    receipt[CONTROL_FIELD]["version"] = 2
    assert parse_delivery_receipt(receipt) is None


def test_missing_sender_or_recipient_does_not_create_receipt():
    assert build_delivery_receipt({"id": new_ulid(), "from": ""}, ["bob"]) is None
    assert build_delivery_receipt({"id": new_ulid(), "from": "alice"}, []) is None
