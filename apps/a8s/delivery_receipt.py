"""Extension-only delivery receipts for remote a8s envelopes.

Receipts retain the normal envelope fields and add ``a8s_control``.  The
reserved destination is deliberately not a participant: older subscribers
drop the envelope, while upgraded subscribers consume it before routing.

Version 2 reports the inbox write and each attachment the receiver could not
fetch. The attachment leg is per recipient because the download is per
recipient, so one recipient holding bytes and another holding nothing is
representable in one conversation between two nodes. Receipts are for the
operator: each is sent once, and nothing waits for one.
"""
from __future__ import annotations

import hashlib
import platform
from dataclasses import dataclass
from datetime import datetime, timezone

from ar3.ulid import is_ulid, new as new_ulid


CONTROL_FIELD = "a8s_control"
CONTROL_TYPE = "delivery_receipt"
CONTROL_VERSION = 2
RECEIPT_TARGET = "__a8s_receipt__"

# The stages a receiver can report. `inbox_write` is delivery;
# `attachment_failed` names the files a recipient will never hold. A node that
# owns none of a message's recipients reports nothing: a8s is best effort, and
# a receipt with any other stage, such as one from a node on an older release,
# is dropped quietly.
STAGES: tuple[str, ...] = ("inbox_write", "attachment_failed")


@dataclass(frozen=True)
class DeliveryReceipt:
    receipt_id: str
    for_id: str
    sender: str
    recipients: tuple[str, ...]
    stage: str
    files: tuple[str, ...] = ()
    detail: str = ""
    node: str = ""


def local_node() -> str:
    """The id this machine puts on the receipts it sends: a short digest of the
    host name, so the receipts of several nodes tell apart without naming any."""
    return "n-" + hashlib.sha256(platform.node().encode()).hexdigest()[:8]


def is_control_envelope(message: dict) -> bool:
    return CONTROL_FIELD in message


def build_delivery_receipt(
    original: dict,
    recipients: list[str],
    stage: str = "inbox_write",
    *,
    files: list[str] | None = None,
    detail: str = "",
    node: str = "",
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
    control_node = str(node or "").strip()
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
            **({"node": control_node} if control_node else {}),
        },
    }


def is_other_stage_receipt(message: dict) -> bool:
    """A receipt of this version whose stage this release does not send."""
    control = message.get(CONTROL_FIELD)
    return (
        isinstance(control, dict)
        and control.get("type") == CONTROL_TYPE
        and control.get("version") == CONTROL_VERSION
        and control.get("stage") not in STAGES
    )


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
    node = control.get("node")
    return DeliveryReceipt(
        receipt_id=receipt_id,
        for_id=for_id,
        sender=sender.strip(),
        recipients=tuple(dict.fromkeys(name.strip() for name in recipients)),
        stage=stage,
        files=files,
        detail=detail.strip() if isinstance(detail, str) else "",
        node=node.strip() if isinstance(node, str) else "",
    )
