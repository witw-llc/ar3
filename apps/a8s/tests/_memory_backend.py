"""An in-memory startup backend: entries live in a dict, so a test can read
what autostart wrote and make the OS refuse."""
from __future__ import annotations

import autostart

STORE: dict[str, autostart.Entry] = {}


class MemoryBackend(autostart.Backend):
    name = "memory"
    event = "test event"
    foreground = False
    fail_install = False
    fail_remove = False

    def __init__(self, store: dict | None = None):
        self.store = STORE if store is None else store

    def install(self, spec):
        if self.fail_install:
            raise autostart.AutostartError("the OS rejected the write")
        self.store[spec.node] = autostart.Entry(spec.node, spec.epoch, " ".join(spec.argv), tuple(spec.argv))

    def remove(self, node, unload=False):
        if self.fail_remove:
            raise autostart.AutostartError("the OS rejected the removal")
        self.store.pop(node, None)

    def entries(self):
        return dict(self.store)
