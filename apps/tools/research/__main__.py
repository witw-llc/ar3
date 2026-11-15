"""Entry point for `python3 apps/tools/research`."""
from __future__ import annotations

import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent.parent
_LIB_DIR = _TOOLS_DIR.parents[1] / "lib"
for _entry in (str(_TOOLS_DIR), str(_LIB_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from research.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
