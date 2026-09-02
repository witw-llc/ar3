"""Tests for the `ar3` foundation package's vendoring hook.

`ensure_vendor()` (ar3/vendor.py) prepends `ar3/_vendor` to `sys.path` so a
vendored import like `paho.mqtt.client` resolves to the copy the suite
tested rather than whatever a system or venv install happens to provide.
These tests run the hook in a subprocess with `-S` (skip `site` — no
site-packages on `sys.path`) so a system-installed paho-mqtt in this
machine's own site-packages can't mask a broken hook and produce a false
pass.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LIB = _REPO_ROOT / "lib"
_VENDOR_DIR = _LIB / "ar3" / "_vendor"
_VENDOR_TXT = _VENDOR_DIR / "vendor.txt"
_VENDORED_PAHO_INIT = _VENDOR_DIR / "paho" / "mqtt" / "__init__.py"


def _run(script: str, env_overrides: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-S", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


class TestEnsureVendor:
    def test_vendored_paho_resolves_under_ar3_vendor(self):
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(_LIB)!r})\n"
            "from ar3.vendor import ensure_vendor\n"
            "ensure_vendor()\n"
            "import paho\n"
            "import paho.mqtt.client\n"
            "print(paho.__file__)\n"
        )
        result = _run(script, {})
        assert result.returncode == 0, result.stderr
        paho_file = Path(result.stdout.strip()).resolve()
        assert paho_file.is_relative_to(_VENDOR_DIR), (
            f"expected paho imported from under {_VENDOR_DIR}, got {paho_file}"
        )

    def test_import_paho_without_hook_fails_under_scrubbed_site(self):
        """Sanity check on the harness itself: with `-S` and no vendor
        insertion, `paho` is not importable at all — proving the previous
        test's pass is attributable to the hook, not to a leaked system
        install riding along in the subprocess."""
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(_LIB)!r})\n"
            "import paho.mqtt.client\n"
        )
        result = _run(script, {})
        assert result.returncode != 0
        assert "paho" in result.stderr

    def test_ar3_no_vendor_leaves_sys_path_untouched(self):
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(_LIB)!r})\n"
            "from ar3.vendor import ensure_vendor\n"
            "before = list(sys.path)\n"
            "ensure_vendor()\n"
            "after = list(sys.path)\n"
            "print('SAME' if before == after else 'CHANGED')\n"
            "print(any('_vendor' in p for p in after))\n"
        )
        result = _run(script, {"AR3_NO_VENDOR": "1"})
        assert result.returncode == 0, result.stderr
        lines = result.stdout.strip().splitlines()
        assert lines[0] == "SAME"
        assert lines[1] == "False"

    def test_ensure_vendor_is_idempotent(self):
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(_LIB)!r})\n"
            "from ar3.vendor import ensure_vendor\n"
            "ensure_vendor()\n"
            "first = list(sys.path)\n"
            "ensure_vendor()\n"
            "second = list(sys.path)\n"
            "print('SAME' if first == second else 'CHANGED')\n"
        )
        result = _run(script, {})
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "SAME"


class TestVendorPin:
    def test_vendor_txt_pin_matches_vendored_paho_version(self):
        assert _VENDOR_TXT.exists(), f"missing {_VENDOR_TXT}"
        text = _VENDOR_TXT.read_text(encoding="utf-8")
        pin_match = re.search(r"^paho-mqtt==(\S+)$", text, re.MULTILINE)
        assert pin_match, f"no paho-mqtt==X.Y.Z pin found in {_VENDOR_TXT}"
        pinned_version = pin_match.group(1)

        assert _VENDORED_PAHO_INIT.exists(), f"missing {_VENDORED_PAHO_INIT}"
        init_text = _VENDORED_PAHO_INIT.read_text(encoding="utf-8")
        version_match = re.search(
            r'__version__\s*=\s*["\']([^"\']+)["\']', init_text
        )
        assert version_match, f"no __version__ found in {_VENDORED_PAHO_INIT}"
        vendored_version = version_match.group(1)

        assert pinned_version == vendored_version, (
            f"vendor.txt pins paho-mqtt=={pinned_version} but the vendored "
            f"copy reports __version__={vendored_version!r} — VERSION drift"
        )

    def test_vendor_txt_has_sha256_line(self):
        text = _VENDOR_TXT.read_text(encoding="utf-8")
        sha_match = re.search(r"^sha256=([0-9a-f]{64})$", text, re.MULTILINE)
        assert sha_match, f"no sha256=<64 hex chars> line found in {_VENDOR_TXT}"


class TestMqttTransportUsesVendorHook:
    def test_mqtt_transport_module_calls_ensure_vendor_before_import(self):
        """The wiring in transports/mqtt.py: `ar3.vendor.ensure_vendor()` runs
        before `import paho.mqtt.client`, guarded by try/except ImportError so
        a relocated copy without the `ar3` package still degrades to the
        pre-existing behavior (system paho, or WARN-skip if truly absent)."""
        mqtt_py = _REPO_ROOT / "apps" / "a8s" / "transports" / "mqtt.py"
        text = mqtt_py.read_text(encoding="utf-8")
        ensure_idx = text.index("ensure_vendor()")
        import_idx = text.index("import paho.mqtt.client as mqtt")
        assert ensure_idx < import_idx, (
            "ensure_vendor() must run before `import paho.mqtt.client`"
        )
