"""Like mock_cli but sleeps first so attached_loop tests can observe in-flight
wakes.

Python rather than bash for the reason in mock_cli.py.
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

time.sleep(float(os.environ.get("MOCK_SLEEP") or 1))
for arg in sys.argv[1:]:
    print(f"MOCK-CLI: {arg}")
