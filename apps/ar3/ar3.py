#!/usr/bin/env python3
"""ar3 — the front door to The Ark (a8s, r4t, k7e).

ar3 orients and verifies. It reads state passively and probes prerequisites;
it never mutates anything and never wraps another product's verbs. There is no
`ar3 tell`, no `ar3 dispatch`, no passthrough: every action belongs to the CLI
that owns it, and ar3's job is to tell you which command that is.

Home resolution mirrors each product exactly (A8S_HOME / R4T_HOME / K7E_HOME),
so ark reports on the same state the products themselves would use.
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
    override = os.environ.get("A8S_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    config = Path.home() / ".config" / "a8s"
    legacy = Path.home() / ".a8s"
    if config.is_dir():
        return config
    if legacy.is_dir():
        return legacy
    return config


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
    raw = os.environ.get("R4T_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "r4t"


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
    raw = os.environ.get("K7E_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "k7e"


def k7e_rows() -> list[Row]:
    home = k7e_home()
    rows = [_cli_row("k7e")]
    nodes = home / "nodes"
    if not nodes.is_dir():
        rows.append((False, "store", f"no store at {home}", "k7e init"))
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
    for group in (HARNESS, SERVICES, TOOLING):
        print()
        print(group)
        _print_rows(doctor_rows(results, group))
    failed = doctor_failures(results)
    green = sum(1 for _check, probe in results if probe.ok)
    print()
    if failed:
        print(f"✗ core prerequisites missing: {', '.join(failed)}  ({green}/{len(results)} probes green)")
        return 1
    print(f"✓ core prerequisites satisfied  ({green}/{len(results)} probes green)")
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
    parser.set_defaults(func=cmd_default)
    sub = parser.add_subparsers(dest="command")
    doctor = sub.add_parser("doctor", help="Probe harness and tool prerequisites")
    doctor.set_defaults(func=cmd_doctor)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
