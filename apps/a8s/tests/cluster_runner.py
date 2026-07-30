"""Subprocess entry: `attached_loop` for one A8S_HOME (integration tests only)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG))

if __name__ == "__main__":
    home, agent, interval, drain = sys.argv[1:5]
    os.environ["A8S_HOME"] = home
    from daemon import attached_loop

    raise SystemExit(attached_loop([agent], float(interval), drain_seconds=float(drain)))
