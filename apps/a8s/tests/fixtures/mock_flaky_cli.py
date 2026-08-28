"""Mock agent CLI that fails on purpose so wake-retry tests can exercise the
nonzero-exit path. Exits $MOCK_FAIL_RC (default 3) unless $MOCK_OK_FILE names
an existing file, in which case it behaves like mock_cli — that lets a test
flip a broken CLI into a working one mid-loop and watch delivery recover.

Python rather than bash for the reason in mock_cli.py.
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

ok_file = os.environ.get("MOCK_OK_FILE")
if ok_file and os.path.exists(ok_file):
    for arg in sys.argv[1:]:
        print(f"MOCK-CLI: {arg}")
    sys.exit(0)

print("MOCK-CLI: deliberate failure")
sys.exit(int(os.environ.get("MOCK_FAIL_RC") or 3))
