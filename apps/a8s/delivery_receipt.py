"""Extension-only delivery receipts for remote a8s envelopes.

Receipts retain the normal envelope fields and add ``a8s_control``.  The
reserved destination is deliberately not a participant: older subscribers
drop the envelope, while upgraded subscribers consume it before routing.

Version 2 reports every stage of a message's life, not only the inbox write.
The attachment leg is per recipient because the download is per recipient, so
one recipient holding bytes and another holding nothing is representable in
one conversation between two nodes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ar3.ulid import is_ulid, new as new_ulid


CONTROL_FIELD = "a8s_control"
CONTROL_TYPE = "delivery_receipt"
CONTROL_VERSION = 2
RECEIPT_TARGET = "__a8s_receipt__"

# The stages a receiver can report. `inbox_write` is delivery; the attachment
# pair is per file; `deferred` is custody without delivery; `expired` and
# `no_local_recipient` are the two ways a message ends with nobody holding it.
STAGES: tuple[str, ...] = (
    "inbox_write",
    "attachment_fetched",
    "attachment_failed",
    "deferred",
    "expired",
    "no_local_recipient",
)

# Stages after which nothing more will arrive for that recipient. A sender
# waiting on an outcome stops here; everything else is still in flight.
# `no_local_recipient` is in neither set on purpose. On a shared topic every
# node that owns none of the recipients reports one, including nodes that were
# never meant to deliver — it is evidence that a node owns nobody, not a
# verdict on the message. Only the absence of any delivery makes it terminal,
# which is the window, not this stage.
TERMINAL_STAGES: frozenset[str] = frozenset(
    {"inbox_write", "attachment_failed", "expired"}
)

FAILURE_STAGES: frozenset[str] = frozenset({"attachment_failed", "expired"})


@dataclass(frozen=True)
class DeliveryReceipt:
    receipt_id: str
    for_id: str
    sender: str
    recipients: tuple[str, ...]
    stage: str
    files: tuple[str, ...] = ()
    detail: str = ""


def is_control_envelope(message: dict) -> bool:
    return CONTROL_FIELD in message


def build_delivery_receipt(
    original: dict,
    recipients: list[str],
    stage: str = "inbox_write",
    *,
    files: list[str] | None = None,
    detail: str = "",
) -> dict | None:
    """Return a receipt envelope, or None when the original cannot correlate."""
    original_id = original.get("id")
    sender = original.get("from")
    clean_recipients = tuple(dict.fromkeys(name.strip() for name in recipients if name.strip()))
    if not isinstance(original_id, str) or not is_ulid(original_id):
        return None
    if not isinstance(sender, str) or not sender.strip() or not clean_recipients:
        return None
    if stage not in STAGES:
        return None
    named = [str(name).strip() for name in (files or []) if str(name).strip()]
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
            "stage": stage,
            "files": named,
            "detail": str(detail or ""),
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
    if stage not in STAGES:
        return None
    # `files` and `detail` are the stage's evidence, not its identity: a
    # receipt naming no file is still a valid receipt, so a malformed one
    # degrades to empty rather than dropping a delivery confirmation.
    raw_files = control.get("files")
    files: tuple[str, ...] = ()
    if isinstance(raw_files, list):
        files = tuple(
            name.strip() for name in raw_files
            if isinstance(name, str) and name.strip()
        )
    detail = control.get("detail")
    return DeliveryReceipt(
        receipt_id=receipt_id,
        for_id=for_id,
        sender=sender.strip(),
        recipients=tuple(dict.fromkeys(name.strip() for name in recipients)),
        stage=stage,
        files=files,
        detail=detail.strip() if isinstance(detail, str) else "",
    )
