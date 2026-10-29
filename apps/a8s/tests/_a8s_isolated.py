"""Run apps/a8s/a8s.py with the account lock directory redirected, so a test's
child process never touches the real account's lock path."""
import os
import runpy
import sys
from pathlib import Path

A8S_DIR = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(A8S_DIR), str(A8S_DIR.parent.parent / "lib")]

import autostart  # noqa: E402

_root = Path(os.environ["A8S_TEST_LOCK_ROOT"])
autostart.account_lock_root = lambda: _root

sys.argv[0] = str(A8S_DIR / "a8s.py")
runpy.run_path(sys.argv[0], run_name="__main__")
