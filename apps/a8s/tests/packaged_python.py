"""Whether this interpreter starts outside a Windows job object.

A packaged (MSIX, for example Microsoft Store) Python activated through its
app execution alias under %LOCALAPPDATA%\\Microsoft\\WindowsApps, or a venv
launcher whose base is such a packaged Python, starts the real python outside
the caller's job, and everything it starts with it. A test whose wake is
`sys.executable` and which needs the wake's processes in the job cannot pass
there. The packaged base run directly stays in the job."""
from __future__ import annotations

import os
import sys

import pytest


def activated_through_a_packaged_launcher() -> bool:
    if os.name != "nt":
        return False
    local = os.environ.get("LOCALAPPDATA", "")
    alias = os.path.join(local, "Microsoft", "WindowsApps").lower() + os.sep
    if local and os.path.abspath(sys.executable).lower().startswith(alias):
        return True
    base = getattr(sys, "_base_executable", "") or ""
    return sys.prefix != sys.base_prefix and "\\windowsapps\\" in base.lower()


launcher_led = pytest.mark.skipif(
    activated_through_a_packaged_launcher(),
    reason="the wake is sys.executable, started through a WindowsApps alias or a venv "
    "of a packaged (Store) Python, which starts the real python outside the wake's "
    "job; apps/ar3/tests/test_ar3_windows_job.py::"
    "test_native_a_cmd_leader_s_orphan_ends_with_its_job covers the job here",
)
