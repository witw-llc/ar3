"""Run a8s.py as a detached start child that stops at a file barrier just
before its pid claim: it writes `ready` in $A8S_TEST_BARRIER, waits for `go`,
then claims through the real code."""
import os
import runpy
import sys
import time
from pathlib import Path

A8S_DIR = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(A8S_DIR), str(A8S_DIR.parent.parent / "lib")]

import daemon  # noqa: E402

_barrier = Path(os.environ["A8S_TEST_BARRIER"])
_real = daemon.claim_without_takeover


def _held_claim(*args, **kwargs):
    (_barrier / f"ready.{os.getpid()}").write_text("")
    while not (_barrier / "go").exists():
        time.sleep(0.02)
    return _real(*args, **kwargs)


daemon.claim_without_takeover = _held_claim

sys.argv[0] = str(A8S_DIR / "a8s.py")
runpy.run_path(sys.argv[0], run_name="__main__")
