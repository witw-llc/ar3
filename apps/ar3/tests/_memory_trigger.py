"""An in-memory auto-update scheduler: what the suite installs instead of an
OS entry, so no test reads or writes the host's crontab, LaunchAgents or tasks."""
from __future__ import annotations

import autoupdate_trigger as trigger_mod

STORE: dict[str, dict] = {}


class MemoryTrigger(trigger_mod.Trigger):
    name = "memory"
    fail_install = False
    unavailable = False

    def install(self, install_id, launcher, python, env):
        if self.unavailable:
            raise trigger_mod.TriggerUnavailable("no scheduler here")
        if self.fail_install:
            raise trigger_mod.TriggerError("scheduler refused the entry")
        argv = trigger_mod.command(launcher, python)
        STORE[install_id] = {"launcher": str(launcher), "python": python, "env": dict(env), "argv": argv}
        return ""

    def remove(self, install_id):
        STORE.pop(install_id, None)

    def installed(self, install_id):
        if self.unavailable:
            raise trigger_mod.TriggerUnavailable("no scheduler here")
        return "memory" if install_id in STORE else None
