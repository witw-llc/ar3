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


def visible_a8s_names() -> dict[str, str]:
    """Every name an outward `tell` from this host resolves, by what it is.

    Asked of a8s rather than read off disk: the registry's shape is pre-v1 and
    may be rebuilt, but `ls` / `aliases` / `namespaces` are the contract. An
    unreachable or unreadable a8s is not an error here — the caller is
    warning about a name collision, and no registry means nothing to collide
    with.
    """
    kinds = {"ls": "node", "aliases": "alias", "namespaces": "namespace"}
    found: dict[str, str] = {}
    for command, kind in kinds.items():
        # No registered nodes means no registry worth asking twice more about.
        if command != "ls" and not found:
            break
        argv = [sys.executable, str(A8S_PY), command]
        if command == "ls":
            argv.append("-q")
        try:
            res = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if res.returncode != 0:
            continue
        for line in (res.stdout or "").splitlines():
            # `ls -q` is bare names; the other two lead with the name and
            # then describe it (`local  [neil-macbook]`, `silo -> silo-node`).
            name = line.strip().split()[0] if line.strip() else ""
            if name:
                found.setdefault(name, kind)
    return found


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
