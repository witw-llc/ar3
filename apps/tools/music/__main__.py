#!/usr/bin/env python3
"""Entry point for `python apps/tools/music`."""
from __future__ import annotations

import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
_LIB_DIR = _TOOLS_DIR.parent.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from music.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
