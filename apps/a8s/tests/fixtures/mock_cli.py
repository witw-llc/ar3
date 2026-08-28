"""Deterministic mock agent CLI used in pytest. Echoes every argv element on
its own line, prefixed with "MOCK-CLI:" so tests can grep for them. Exits 0.

Python rather than bash, and invoked through `$PYTHON` (or `sys.executable`)
rather than by bare path: these fixtures are named by explicit path, so Windows
cannot append a `.cmd` and cannot exec a `#!` file. It raised OSError
[WinError 193], which is not the FileNotFoundError `_start_wake_subprocess`
catches, so it surfaced as an uncaught error — 61 native a8s tests, the largest
single Windows cause in this suite.
"""
import sys

# The daemon reads this pipe as utf-8; Windows would otherwise encode to the
# console code page and mangle anything outside it.
sys.stdout.reconfigure(encoding="utf-8")

for arg in sys.argv[1:]:
    print(f"MOCK-CLI: {arg}")
