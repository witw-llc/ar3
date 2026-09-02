"""The reserved-env contract a8s routing owns, shared by every app that must
not let a rig or config file silently override it.

`TELL_OUTBOX_DIR` and `TELL_FILE_MAX` are both values a8s computes and
injects on wake (the outbox a turn writes into, the attachment cap the
routing layer enforces) — a rig config that names either from underneath
would silently desync the harness from what routing actually does. r4t's own
turn machinery adds `PWD` on top (see `rig.TURN_OWNED_ENV`): the workdir it
pins per member, not something a8s routing knows about.
"""
from __future__ import annotations

TELL_OUTBOX_DIR_ENV = "TELL_OUTBOX_DIR"
TELL_FILE_MAX_ENV = "TELL_FILE_MAX"

ROUTING_OWNED = (TELL_OUTBOX_DIR_ENV, TELL_FILE_MAX_ENV)
