from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeAlias

TellFn: TypeAlias = Callable[[str, str], None]

SIMULATE_ENV = "R4T_SIMULATE_TELL"


# The `tell` on PATH is an operator convenience installed by install.sh; a
# dispatch host (CI runner, container, fresh Linux box) has no such PATH entry,
# so r4t resolves its sibling a8s entry point absolutely.
A8S_PY = Path(__file__).resolve().parent.parent / "a8s" / "a8s.py"


def default_tell(agent: str, body: str) -> None:
    subprocess.run([sys.executable, str(A8S_PY), "tell", agent, body], check=False)


def simulate_tell(agent: str, body: str) -> None:
    print(f"r4t> tell {agent}:", file=sys.stderr)
    for line in body.splitlines():
        print(f"r4t>   {line}", file=sys.stderr)


def noop_tell(_agent: str, _body: str) -> None:
    return None


def resolve_tell_fn(*, notify: bool, simulate: bool) -> TellFn:
    if simulate:
        return simulate_tell
    if notify:
        return default_tell
    return noop_tell


def simulate_enabled(flag: bool) -> bool:
    if flag:
        return True
    raw = os.environ.get(SIMULATE_ENV, "").strip().lower()
    return raw in ("1", "true", "yes", "on")
