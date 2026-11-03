"""A test-suite guard: the suite must not write the developer's real state.

A fixture that points the homes at a tmp dir keeps tests honest only while
every test is behind it. This is the second wall. It records the real
`~/.config/<app>` trees when the session starts and compares them when it ends;
a suite that touched one fails the run and names the paths. The homes are
resolved from the environment the session was LAUNCHED with, before any fixture
redirects them.

The floors are the prevention (every test starts with every home variable at a
throwaway dir); this guard is the detection. A live seat on the same machine
writing the same tree during the run (a quota snapshot, a day log) trips it
too, so:

- `CI` set, or `AR3_TESTS_STRICT_HOME=1`: a touched real tree FAILS the run
  (exit 1).
- otherwise: a warning on stderr listing the paths, and the run's own result
  stands.
- `AR3_TESTS_ALLOW_REAL_HOME=1`: the guard is off.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Held at import: a suite that bans `subprocess.run` for its tests (ar3's does)
# must still be able to launch the nested run below.
_RUN = subprocess.run

ENV_VARS = {"a8s": "A8S_HOME", "r4t": "R4T_HOME", "k7e": "K7E_HOME"}

# Every variable that decides where a suite's state lands. A suite floors ALL
# of them for each test; a variable it forgot lets an inherited override (a
# launcher's live store) survive fixture setup.
HOME_VARS = ("A8S_HOME", "R4T_HOME", "K7E_HOME", "AR3_HOME", "XDG_CONFIG_HOME")

# As launched, before any fixture ran.
LAUNCHED = {name: os.environ.get(name) for name in HOME_VARS}


def throwaway_homes(root: Path) -> dict[str, str]:
    """A throwaway value for every home variable, under `root`."""
    return {
        "A8S_HOME": str(root / "a8s"), "R4T_HOME": str(root / "r4t"),
        "K7E_HOME": str(root / "k7e"), "AR3_HOME": str(root / "ar3"),
        "XDG_CONFIG_HOME": str(root / "xdg"),
    }


def still_launched() -> list[str]:
    """Home variables that, right now, still hold the value the session was
    launched with. Empty means every inherited override has been replaced."""
    return [
        name for name, value in LAUNCHED.items()
        if value and os.environ.get(name) == value
    ]


def sentinel_run(argv: list[str], cwd: Path, root: Path) -> tuple[int, str, list[str]]:
    """Run `argv` (a nested pytest) with every home variable set to a sentinel
    dir under `root`, as a launcher with a live store would. Returns the exit
    code, the output and the files the run left in any sentinel dir."""
    env = dict(os.environ)
    for name in HOME_VARS:
        env[name] = str(root / name)
    env.pop("AR3_TESTS_ALLOW_REAL_HOME", None)
    done = _RUN(
        argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=300
    )
    written = sorted(
        str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
    ) if root.is_dir() else []
    return done.returncode, done.stdout + done.stderr, written


def real_home(app: str) -> Path:
    """Where `app` keeps its state for the environment as launched."""
    override = (os.environ.get(ENV_VARS.get(app, "")) or "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / app


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """{relative path: (size, mtime_ns)} for every file under `root`."""
    found: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return found
    for base, _dirs, names in os.walk(root):
        for name in names:
            path = Path(base) / name
            try:
                info = path.stat()
            except OSError:
                continue
            found[str(path.relative_to(root))] = (info.st_size, info.st_mtime_ns)
    return found


def changes(before: dict, after: dict) -> list[str]:
    """Human lines for what was added, removed or rewritten."""
    lines = [f"added    {p}" for p in sorted(after.keys() - before.keys())]
    lines += [f"removed  {p}" for p in sorted(before.keys() - after.keys())]
    lines += [
        f"changed  {p}" for p in sorted(before.keys() & after.keys())
        if before[p] != after[p]
    ]
    return lines


class HomeGuard:
    def __init__(self, apps: tuple[str, ...]):
        self.roots = {app: real_home(app) for app in apps}
        self.before = {app: snapshot(root) for app, root in self.roots.items()}

    def report(self) -> list[str]:
        out: list[str] = []
        for app, root in self.roots.items():
            out += [f"{root}: {line}" for line in changes(self.before[app], snapshot(root))]
        return out

    def finish(self, session) -> None:
        """Call from `pytest_sessionfinish`. A real home that moved fails the
        run under `CI` or `AR3_TESTS_STRICT_HOME=1`, and is a warning
        otherwise."""
        if os.environ.get("AR3_TESTS_ALLOW_REAL_HOME") == "1":
            return
        moved = self.report()
        if not moved:
            return
        strict = bool(os.environ.get("CI")) or os.environ.get("AR3_TESTS_STRICT_HOME") == "1"
        print(
            ("\nREAL STATE TOUCHED" if strict else "\nwarning: real state touched")
            + ": a real home moved during this run (a test wrote outside its tmp "
            "homes, or a live seat wrote the same tree"
            + ("" if strict else "; CI or AR3_TESTS_STRICT_HOME=1 makes this a failure")
            + "; AR3_TESTS_ALLOW_REAL_HOME=1 silences it):\n  "
            + "\n  ".join(moved[:40]),
            file=sys.stderr,
        )
        if strict:
            session.exitstatus = 1
