"""On-demand heavy dependencies — the tier-2 half of the suite's dependency
foundation. `ark/vendor.py` ships small, pinned, always-present packages
inside the repo; this module is the opposite shape: a group named by a
`requirements/<group>.txt` file (boto3 for S3 storage, textual for the r4t
TUI) that most installs never touch and that only `ar3 deps <group>` (never
an import site) ever fetches.

Installs land under `deps_root()/interpreter_key()/<group>/`, one directory
per Python build. The interpreter key folds in both the ABI tag and the
platform tag, so a Python upgrade or a machine move changes the key and the
old directory is simply never looked at again — a clean miss instead of a
half-compatible `.so` crashing an import. Nothing here ever deletes an old
key's directory; that is left for the user to prune.

`ensure_group` only ever reads — it degrades a caller with a WARN naming the
fix (`ar3 deps <group>`) rather than installing anything itself, so an
import site can call it unconditionally without turning a read into a write.
Installing is `install_group`'s job alone, invoked by the `ar3 deps` verb.
`use_group` is the one-line call an import site makes: ensure, then insert
the group dir into `sys.path` ahead of site-packages/dist-packages but
behind stdlib and the vendored dir — a fetched group beats an older copy of
the same package already installed system- or venv-wide, while stdlib and
`ark.vendor`'s prepend-at-0 stay authoritative over anything fetched.
"""
from __future__ import annotations

import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

from ark.ulid import new as new_ulid

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS_DIR = REPO_ROOT / "requirements"


def deps_root() -> Path:
    """Base directory fetched dependency groups live under:
    `XDG_DATA_HOME/ark/deps` (or `~/.local/share/ark/deps` when unset) —
    the same override-then-default shape `ark.home.app_home` uses for
    `XDG_CONFIG_HOME`, applied to the data-home variable instead."""
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "ark" / "deps"


def interpreter_key() -> str:
    """Identify the running Python build: ABI/cache tag plus platform tag,
    e.g. `cpython-314-macosx-26.0-arm64`. Any interpreter upgrade or machine
    move changes this string, so a stale install's directory simply misses
    on the next lookup instead of half-loading incompatible extension
    modules."""
    return f"{sys.implementation.cache_tag}-{sysconfig.get_platform()}"


def group_dir(group: str) -> Path:
    """Where `group`'s fetched packages live for the running interpreter."""
    return deps_root() / interpreter_key() / group


def _requirements_file(group: str) -> Path:
    return REQUIREMENTS_DIR / f"{group}.txt"


def known_groups() -> list[str]:
    """Group names with a `requirements/<group>.txt` pin file, excluding the
    `*-test.txt` files that pin test tooling rather than a fetched group."""
    if not REQUIREMENTS_DIR.is_dir():
        return []
    return sorted(
        p.stem for p in REQUIREMENTS_DIR.glob("*.txt")
        if not p.stem.endswith("-test")
    )


_REQ_LINE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)$")
_CLAUSE_RE = re.compile(r"(==|!=|>=|<=|>|<)\s*([A-Za-z0-9.]+)")
_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


def _parse_requirements(path: Path) -> list[tuple[str, str]]:
    """`[(name, version_specifier)]` for each real requirement line — blank
    lines, comments, and `-r`/`-e`/other flag lines are skipped. Not a full
    PEP 508 parser: enough to check the pins this suite's own
    `requirements/*.txt` files actually use."""
    pins = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQ_LINE_RE.match(line)
        if not match:
            continue
        pins.append((match.group(1), match.group(2).strip()))
    return pins


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version)
    return tuple(int(p) for p in parts) if parts else (0,)


def _satisfies(installed_version: str, specifier: str) -> bool:
    if not specifier:
        return True
    installed = _version_tuple(installed_version)
    for op, ver in _CLAUSE_RE.findall(specifier):
        if not _OPS[op](installed, _version_tuple(ver)):
            return False
    return True


def _installed_versions(dir_: Path) -> dict[str, str]:
    versions = {}
    for dist in importlib.metadata.distributions(path=[str(dir_)]):
        name = dist.metadata.get("Name") if dist.metadata else None
        if name:
            versions[name.lower()] = dist.version
    return versions


def ensure_group(group: str) -> Path | None:
    """`group_dir(group)` if it already exists and satisfies every pin in
    `requirements/<group>.txt`, else `None`. Never installs, never writes —
    a caller on a hot import path can call this unconditionally. The check
    is a directory listing plus one `importlib.metadata` scan of that
    directory alone (not `sys.path`), so it stays cheap even called on every
    process start."""
    dir_ = group_dir(group)
    if not dir_.is_dir():
        return None
    req_file = _requirements_file(group)
    if not req_file.is_file():
        return dir_
    installed = _installed_versions(dir_)
    for name, specifier in _parse_requirements(req_file):
        version = installed.get(name.lower())
        if version is None or not _satisfies(version, specifier):
            return None
    return dir_


def install_group(group: str) -> Path:
    """Install `requirements/<group>.txt` into `group_dir(group)`.

    Resolves `uv` first (`uv pip install --target <dir> -r <file>`), falling
    back to `<sys.executable> -m pip install --target <dir> -r <file>` when
    `uv` is not on PATH. `--target` is what makes either command work on a
    PEP 668 (externally-managed) system: it installs into a plain directory
    rather than the interpreter's own site-packages, so there is no
    system/venv Python to protect and no `--break-system-packages` flag to
    reach for.

    Installs into a sibling temp directory first, and only swaps it into
    `group_dir(group)` — via `os.replace`, moving the previous install aside
    first and cleaning it up after — once the installer exits 0. A failed
    install therefore never leaves a half-populated group dir, and the
    previous good install (if any) survives a failed re-install untouched.
    """
    req_file = _requirements_file(group)
    if not req_file.is_file():
        raise FileNotFoundError(f"no such dependency group: {req_file} does not exist")
    final_dir = group_dir(group)
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = final_dir.parent / f".{group}.{new_ulid()}.tmp"
    installer = shutil.which("uv")
    argv = (
        [installer, "pip", "install", "--target", str(tmp_dir), "-r", str(req_file)]
        if installer
        else [sys.executable, "-m", "pip", "install", "--target", str(tmp_dir), "-r", str(req_file)]
    )
    result = subprocess.run(argv)
    if result.returncode != 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(
            f"installing dependency group {group!r} failed (exit {result.returncode}): "
            f"{' '.join(argv)}"
        )
    old_dir = None
    if final_dir.exists():
        old_dir = final_dir.parent / f".{group}.{new_ulid()}.old"
        os.replace(final_dir, old_dir)
    try:
        os.replace(tmp_dir, final_dir)
    except OSError:
        if old_dir is not None:
            os.replace(old_dir, final_dir)
        raise
    finally:
        if old_dir is not None and old_dir.exists():
            shutil.rmtree(old_dir, ignore_errors=True)
    return final_dir


def use_group(group: str) -> bool:
    """Make `group`'s fetched packages importable for the rest of this
    process: `ensure_group` then insert its directory into `sys.path` ahead
    of the first site-packages/dist-packages entry (falling back to append
    when none is found) — stdlib dirs and the vendored dir prepended at
    `sys.path[0]` still precede it, but a fetched group now beats an older
    copy of the same package already installed system- or venv-wide instead
    of losing to it. Returns `False` with a stderr WARN naming `ar3 deps
    <group>` when the group is missing or unsatisfied; the caller's own
    `ImportError` handling then degrades exactly as it did before this
    existed."""
    dir_ = ensure_group(group)
    if dir_ is None:
        print(
            f"WARN: {group!r} dependencies not installed — run `ar3 deps {group}`",
            file=sys.stderr,
        )
        return False
    path_str = str(dir_)
    if path_str not in sys.path:
        for i, entry in enumerate(sys.path):
            if "site-packages" in entry or "dist-packages" in entry:
                sys.path.insert(i, path_str)
                break
        else:
            sys.path.append(path_str)
    return True
