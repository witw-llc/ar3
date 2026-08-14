"""The vendoring hook: prepend `ark/_vendor` to `sys.path`.

Everything under `_vendor/` is an unmodified PyPI release, pinned (with its
verified sha256) in `_vendor/vendor.txt`. `ensure_vendor()` prepends the
vendor directory to `sys.path[0]` so an import of a vendored name (e.g.
`paho.mqtt.client`) resolves to the copy the suite tested, not whatever
happens to be on the system — the same problem pip-free deploys hit, and the
same fix Ansible's own `_vendor` shim uses.

Set `ARK_NO_VENDOR` (any truthy value) to skip the hook entirely, e.g. to
force resolution against a system or venv-installed copy instead.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_VENDOR_DIR = str(Path(__file__).resolve().parent / "_vendor")

# Import names this hook vendors, used only for the already-imported warning.
_VENDORED_TOP_NAMES = ("paho",)


def ensure_vendor() -> None:
    """Prepend `ark/_vendor` to `sys.path` so vendored imports resolve there
    first. Idempotent — skips the insert if the directory is already on
    `sys.path`. No-op entirely when `ARK_NO_VENDOR` is truthy in the
    environment."""
    if os.environ.get("ARK_NO_VENDOR"):
        return
    already_present = _VENDOR_DIR in sys.path
    for name in _VENDORED_TOP_NAMES:
        mod = sys.modules.get(name)
        mod_file = getattr(mod, "__file__", None) if mod else None
        if mod_file and not str(Path(mod_file).resolve()).startswith(_VENDOR_DIR):
            print(
                f"ark.vendor: {name!r} was already imported from {mod_file} "
                "before the vendor hook ran — the vendored copy will not "
                "take effect for it this process",
                file=sys.stderr,
            )
    if already_present:
        return
    sys.path.insert(0, _VENDOR_DIR)
