"""Extension-only delivery receipts for remote a8s envelopes.

Receipts retain the normal envelope fields and add ``a8s_control``.  The
reserved destination is deliberately not a participant: older subscribers
drop the envelope, while upgraded subscribers consume it before routing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ulid import is_ulid, new as new_ulid


CONTROL_FIELD = "a8s_control"
CONTROL_TYPE = "delivery_receipt"
CONTROL_VERSION = 1
RECEIPT_TARGET = "__a8s_receipt__"


@dataclass(frozen=True)
class DeliveryReceipt:
    receipt_id: str
    for_id: str
    sender: str
    recipients: tuple[str, ...]
    stage: str


def is_control_envelope(message: dict) -> bool:
    return CONTROL_FIELD in message


def build_delivery_receipt(original: dict, recipients: list[str]) -> dict | None:
    """Return a receipt envelope, or None when the original cannot correlate."""
    original_id = original.get("id")
    sender = original.get("from")
    clean_recipients = tuple(dict.fromkeys(name.strip() for name in recipients if name.strip()))
    if not isinstance(original_id, str) or not is_ulid(original_id):
        return None
    if not isinstance(sender, str) or not sender.strip() or not clean_recipients:
        return None
    return {
        "id": new_ulid(),
        "date": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "from": "_a8s",
        "to": RECEIPT_TARGET,
        "content": "",
        "files": [],
        CONTROL_FIELD: {
            "type": CONTROL_TYPE,
            "version": CONTROL_VERSION,
            "for_id": original_id,
            "sender": sender.strip(),
            "recipients": list(clean_recipients),
            "stage": "inbox_write",
        },
    }


def parse_delivery_receipt(message: dict) -> DeliveryReceipt | None:
    """Parse the supported receipt extension; reject malformed/unknown control."""
    if message.get("to") != RECEIPT_TARGET or message.get("from") != "_a8s":
        return None
    if message.get("content") != "" or message.get("files") != []:
        return None
    control = message.get(CONTROL_FIELD)
    if not isinstance(control, dict):
        return None
    if control.get("type") != CONTROL_TYPE or control.get("version") != CONTROL_VERSION:
        return None
    receipt_id = message.get("id")
    for_id = control.get("for_id")
    sender = control.get("sender")
    recipients = control.get("recipients")
    stage = control.get("stage")
    if not isinstance(receipt_id, str) or not is_ulid(receipt_id):
        return None
    if not isinstance(for_id, str) or not is_ulid(for_id):
        return None
    if not isinstance(sender, str) or not sender.strip():
        return None
    if not isinstance(recipients, list) or not recipients:
        return None
    if not all(isinstance(name, str) and name.strip() for name in recipients):
        return None
    if stage != "inbox_write":
        return None
    return DeliveryReceipt(
        receipt_id=receipt_id,
        for_id=for_id,
        sender=sender.strip(),
        recipients=tuple(dict.fromkeys(name.strip() for name in recipients)),
        stage=stage,
    )
