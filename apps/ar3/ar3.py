#!/usr/bin/env python3
"""ar3 — the front door to The Ark (a8s, r4t, k7e).

ar3 never mutates product state; it owns and maintains the suite's own
substrate instead. It reads a8s/r4t/k7e state passively and probes
prerequisites — there is no `ar3 tell`, no `ar3 dispatch`, no passthrough:
every action belongs to the CLI that owns it, and ar3's job is to tell you
which command that is. The one exception is `ar3 deps`, which fetches
on-demand heavy dependencies (boto3, textual) into `~/.local/share/ark/deps`:
that directory is substrate ar3 itself owns, not product state, and it is the
only thing ar3 ever writes.

Home resolution imports the same `ark.home.app_home` resolver the products
themselves call (A8S_HOME / R4T_HOME / K7E_HOME), so ar3's reporting can
never go stale against a product's own resolution.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# `arkver` sits at the repo root and carries the suite semver. A copy of this
# tree relocated away from that root (the isolation container copies apps/r4t
# alone to /opt/r4t) still has to run: the version is a nicety, never a
# dependency, so a missing module degrades to "unknown" instead of killing
# the CLI on import.
sys.path.append(str(Path(__file__).resolve().parents[2]))
try:
    from arkver import update_note, version_line  # noqa: E402
except ImportError:
    def version_line(app: str) -> str:
        return f"{app} unknown (The Ark)"

    def update_note(timeout_s: float = 0) -> str:
        return "unknown (no VERSION file beside this copy)"

from ark import deps as ark_deps  # noqa: E402
from ark.home import app_home  # noqa: E402
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

WORDMARK = ("A R K", "8 4 7", "S T E")

TAGLINE = (
    "The Ark — a8s routes the messages, r4t governs the roster,",
    "k7e keeps what they learn. ar3 reads; each product owns its own verbs.",
)

PATH_HINT = f"source {REPO_ROOT}/install.sh"

Row = tuple[Optional[bool], str, str, Optional[str]]


# ---------- panel rendering ----------

def _mark(ok: bool | None) -> str:
    return {True: "✓", False: "✗"}.get(ok, "-")


def render_rows(rows: list[Row]) -> list[str]:
    if not rows:
        return ["  (none)"]
    width = max(len(name) for _ok, name, _state, _hint in rows)
    lines = []
    for ok, name, state, hint in rows:
        line = f"  {_mark(ok)} {name:<{width}}  {state}"
        if hint:
            line += f"   (try: {hint})"
        lines.append(line.rstrip())
    return lines


def _print_rows(rows: list[Row]) -> None:
    for line in render_rows(rows):
        print(line)


# ---------- shared probes ----------

def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _locate(binary: str) -> tuple[Path | None, bool]:
    """(path, on_PATH). A suite CLI sitting beside ar3 but absent from PATH is
    found and reported, because that is the shape of a half-finished install."""
    found = shutil.which(binary)
    if found:
        return Path(found), True
    sibling = REPO_ROOT / binary
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return sibling, False
    return None, False


def _cli_row(binary: str) -> Row:
    path, on_path = _locate(binary)
    if path is None:
        return (False, "cli", f"{binary} not found", PATH_HINT)
    if not on_path:
        return (False, "cli", f"{binary} at {path}, not on PATH", PATH_HINT)
    return (True, "cli", f"{binary} -> {path}", None)


def _run(argv: list[str], timeout: float) -> tuple[int | None, str]:
    """(exit code, combined output). None as the code means it never answered."""
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None, ""
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


# ---------- a8s ----------

def a8s_home() -> Path:
    return app_home("a8s", os.environ.get("A8S_HOME"), legacy=Path.home() / ".a8s")


def _live_pid(path: Path) -> int | None:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except OSError:
        pass
    return pid


def a8s_rows() -> list[Row]:
    home = a8s_home()
    rows = [_cli_row("a8s")]
    registry = home / "a8s.json"
    if not registry.is_file():
        rows.append((False, "registry", f"no registry at {registry}", "a8s discover <dir>"))
        return rows
    data = _read_json(registry)
    if data is None:
        rows.append((False, "registry", f"unreadable: {registry}", f"inspect {registry}"))
        return rows
    agents = data.get("agents") if isinstance(data.get("agents"), dict) else {}
    aliases = data.get("aliases") if isinstance(data.get("aliases"), dict) else {}
    namespaces = data.get("namespaces") if isinstance(data.get("namespaces"), dict) else {}
    detail = f"{len(agents)} agent(s), {len(aliases)} alias(es), {len(namespaces)} namespace(s)"
    rows.append((
        bool(agents), "registry", detail,
        None if agents else "a8s discover <dir>",
    ))
    running = [
        name for name in sorted(agents)
        if _live_pid(home / "agents" / re.sub(r"[^A-Za-z0-9_-]", "_", name) / "pid") is not None
    ]
    if running:
        rows.append((True, "router", f"attached: {', '.join(running)}", None))
    elif agents:
        rows.append((False, "router", "no agent attached", "a8s start <agent>"))
    return rows


# ---------- r4t ----------

def r4t_home() -> Path:
    return app_home("r4t", os.environ.get("R4T_HOME"))


def r4t_rows() -> list[Row]:
    home = r4t_home()
    rows = [_cli_row("r4t")]
    rigs = home / "rigs.json"
    if not rigs.is_file():
        rows.append((False, "rigs", f"no rig config at {rigs}", "r4t init"))
    else:
        data = _read_json(rigs)
        if data is None:
            rows.append((False, "rigs", f"unreadable: {rigs}", f"inspect {rigs}"))
        else:
            names = sorted(
                key for key, value in data.items()
                if not key.startswith("_") and isinstance(value, dict) and "invoke" in value
            )
            rows.append((
                bool(names),
                "rigs",
                f"{len(names)} rig(s): {', '.join(names)}" if names else "no rigs defined",
                None if names else "r4t rig add <rig> <preset>",
            ))
    rosters_dir = home / "rosters"
    rosters = (
        sorted(p.name for p in rosters_dir.iterdir() if p.is_dir())
        if rosters_dir.is_dir() else []
    )
    rows.append((
        bool(rosters),
        "rosters",
        f"{len(rosters)} roster(s): {', '.join(rosters)}" if rosters else f"none under {rosters_dir}",
        None if rosters else "r4t init",
    ))
    return rows


# ---------- k7e ----------

def k7e_home() -> Path:
    return app_home("k7e", os.environ.get("K7E_HOME"))


def k7e_rows() -> list[Row]:
    home = k7e_home()
    rows = [_cli_row("k7e")]
    nodes = home / "nodes"
    if not nodes.is_dir():
        rows.append((False, "store", f"no store at {home}", "k7e store <title>"))
        return rows
    entries = sum(1 for p in nodes.rglob("*.md"))
    rows.append((True, "store", f"{entries} entr(ies) under {nodes}", None))
    index = home / ".index.db"
    if index.is_file():
        size = index.stat().st_size
        rows.append((True, "index", f"{size // 1024} KiB at {index}", None))
    else:
        rows.append((False, "index", "no search index", "k7e reindex"))
    return rows


# ---------- greeter ----------

PRODUCTS = (
    ("a8s", "agent message router", a8s_home, a8s_rows),
    ("r4t", "the roster", r4t_home, r4t_rows),
    ("k7e", "knowledge engine", k7e_home, k7e_rows),
)


def cmd_default(_args: argparse.Namespace) -> int:
    for line in WORDMARK:
        print(line)
    print()
    for line in TAGLINE:
        print(line)
    for name, blurb, home, rows_fn in PRODUCTS:
        print()
        print(f"{name} — {blurb}  ({home()})")
        _print_rows(rows_fn())
    print()
    print("next: ar3 doctor — probe the harnesses and tools the suite runs on")
    return 0


# ---------- doctor ----------

HARNESS = "Harnesses"
SERVICES = "Services"
TOOLING = "Tooling"


@dataclass(frozen=True)
class Probe:
    ok: bool
    detail: str


@dataclass(frozen=True)
class Check:
    name: str
    group: str
    probe: Callable[[], Probe]
    hint: str
    core: bool = False


def _version_probe(binary: str, argv: tuple[str, ...] = ("--version",), timeout: float = 5.0):
    def probe() -> Probe:
        path = shutil.which(binary)
        if path is None:
            return Probe(False, "not on PATH")
        code, out = _run([path, *argv], timeout)
        if code is None:
            return Probe(False, f"no answer in {timeout:g}s — {path}")
        version = _first_line(out)
        if code != 0:
            return Probe(False, f"{' '.join(argv)} exited {code} — {version or path}")
        return Probe(True, f"{version or 'ok'}  ({path})")
    return probe


def _ollama_probe() -> Probe:
    path = shutil.which("ollama")
    if path is None:
        return Probe(False, "not on PATH")
    code, out = _run([path, "list"], 5.0)
    if code is None:
        return Probe(False, "server did not answer in 5s")
    if code != 0:
        return Probe(False, f"server unreachable — {_first_line(out) or f'list exited {code}'}")
    models = [
        line.split()[0] for line in out.splitlines()[1:]
        if line.strip() and not line.startswith("NAME")
    ]
    if not models:
        return Probe(True, "reachable, no models pulled")
    return Probe(True, f"{len(models)} model(s): {', '.join(models)}")


def _docker_probe() -> Probe:
    path = shutil.which("docker")
    if path is None:
        return Probe(False, "not on PATH")
    code, out = _run([path, "info", "--format", "{{.ServerVersion}}"], 8.0)
    if code is None:
        return Probe(False, "daemon did not answer in 8s")
    if code != 0:
        return Probe(False, f"daemon unreachable — {_first_line(out) or f'info exited {code}'}")
    return Probe(True, f"daemon {_first_line(out) or 'reachable'}")


def _git_probe() -> Probe:
    path = shutil.which("git")
    if path is None:
        return Probe(False, "not on PATH")
    code, out = _run([path, "--version"], 5.0)
    if code is None or code != 0:
        return Probe(False, f"--version failed — {path}")
    version = _first_line(out)
    missing = [
        key for key in ("user.name", "user.email")
        if not _first_line(_run([path, "config", "--get", key], 5.0)[1])
    ]
    if missing:
        return Probe(False, f"{version}, unset: {', '.join(missing)}")
    return Probe(True, version)


CHECKS: tuple[Check, ...] = (
    Check("claude", HARNESS, _version_probe("claude"), "install Claude Code"),
    Check("agent", HARNESS, _version_probe("agent"), "install the Cursor agent CLI"),
    Check("codex", HARNESS, _version_probe("codex"), "install the Codex CLI"),
    Check("copilot", HARNESS, _version_probe("copilot"), "install the GitHub Copilot CLI"),
    Check("opencode", HARNESS, _version_probe("opencode"), "install OpenCode"),
    Check("agy", HARNESS, _version_probe("agy"), "install Antigravity"),
    Check("ollama", HARNESS, _version_probe("ollama"), "install ollama"),
    Check("ollama serve", SERVICES, _ollama_probe, "ollama serve, then ollama pull <model>"),
    Check("docker", SERVICES, _docker_probe, "start Docker Desktop or the docker daemon"),
    Check("git", TOOLING, _git_probe, "git config --global user.name / user.email", core=True),
)


def doctor_results(checks: tuple[Check, ...]) -> list[tuple[Check, Probe]]:
    return [(check, check.probe()) for check in checks]


def doctor_rows(results: list[tuple[Check, Probe]], group: str) -> list[Row]:
    return [
        (probe.ok, check.name, probe.detail, None if probe.ok else check.hint)
        for check, probe in results if check.group == group
    ]


def doctor_failures(results: list[tuple[Check, Probe]]) -> list[str]:
    """Core prerequisites that are not satisfied. A suite with no agent harness
    at all cannot run a roster turn, so that counts as core alongside the
    checks flagged `core`."""
    failed = [check.name for check, probe in results if check.core and not probe.ok]
    harnesses = [probe.ok for check, probe in results if check.group == HARNESS]
    if harnesses and not any(harnesses):
        failed.append("at least one agent harness")
    return failed


def cmd_doctor(_args: argparse.Namespace) -> int:
    results = doctor_results(CHECKS)
    print("ar3 doctor — probes only; nothing here is installed, started, or changed")
    # The only probe pointed at the suite itself. It reaches the public mirror,
    # so it is here and not in bare `ar3`, which must stay offline and instant.
    print(f"suite: {update_note()}")
    for group in (HARNESS, SERVICES, TOOLING):
        print()
        print(group)
        _print_rows(doctor_rows(results, group))
    failed = doctor_failures(results)
    green = sum(1 for _check, probe in results if probe.ok)
    # The two symptoms are one story. A harness this shell cannot see is a
    # harness no a8s node started from this shell can see either, unless the
    # node was given a PATH of its own — otherwise the failure lands hours
    # later at a wake, in a shell nobody is watching.
    unseen = [
        check.name
        for check, probe in results
        if check.group == HARNESS and not probe.ok and probe.detail == "not on PATH"
    ]
    if unseen:
        print()
        print(
            f"note: {', '.join(unseen)} not visible from this shell. `a8s start` "
            "here would hand\n      the same PATH to every wake — give a8s a PATH "
            "of its own from a shell that\n      does see them "
            "(`a8s config set wake_path \"$PATH\"`), or set `definition.env`."
        )
    print()
    if failed:
        print(f"✗ core prerequisites missing: {', '.join(failed)}  ({green}/{len(results)} probes green)")
        return 1
    print(f"✓ core prerequisites satisfied  ({green}/{len(results)} probes green)")
    return 0


# ---------- deps ----------

def _deps_status_row(group: str) -> Row:
    dir_ = ark_deps.ensure_group(group)
    if dir_ is not None:
        return (True, group, f"installed at {dir_}", None)
    return (False, group, "not installed", f"ar3 deps {group}")


def cmd_deps(args: argparse.Namespace) -> int:
    group = getattr(args, "group", None)
    groups = ark_deps.known_groups()
    if not group:
        interpreter_dir = ark_deps.deps_root() / ark_deps.interpreter_key()
        print(f"ar3 deps — on-demand heavy dependencies  ({interpreter_dir})")
        print()
        _print_rows([_deps_status_row(g) for g in groups])
        return 0
    if group not in groups:
        known = ", ".join(groups) if groups else "none defined"
        print(f"ar3 deps: no such group {group!r} (known: {known})", file=sys.stderr)
        return 2
    try:
        dest = ark_deps.install_group(group)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"ar3 deps {group}: {e}", file=sys.stderr)
        return 1
    print(f"ar3 deps {group}: installed to {dest}")
    return 0


# ---------- cli ----------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ar3",
        description=(
            "Front door to The Ark. Bare `ar3` reports where a8s, r4t and "
            "k7e stand; `ar3 doctor` probes the tools they need. ar3 never runs "
            "another product's commands for you."
        ),
    )
    parser.add_argument("--version", action="version", version=version_line("ar3"))
    parser.set_defaults(func=cmd_default)
    sub = parser.add_subparsers(dest="command")
    doctor = sub.add_parser("doctor", help="Probe harness and tool prerequisites")
    doctor.set_defaults(func=cmd_doctor)
    deps = sub.add_parser(
        "deps",
        help="List, or install, on-demand heavy dependency groups",
        description=(
            "ar3 deps lists known dependency groups (requirements/*.txt) with "
            "installed/missing status for the running interpreter. ar3 deps "
            "<group> installs that group into ~/.local/share/ark/deps — the "
            "one thing ar3 ever writes."
        ),
    )
    deps.add_argument(
        "group", nargs="?",
        help="Dependency group to install, e.g. a8s-s3 or r4t (see requirements/*.txt)",
    )
    deps.set_defaults(func=cmd_deps)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
