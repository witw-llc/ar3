#!/usr/bin/env python3
"""ar3 — the front door to the ar3 suite (a8s, r4t, k7e).

ar3 never mutates product state; it owns and maintains the suite's own
substrate instead. It reads a8s/r4t/k7e state passively and probes
prerequisites — there is no `ar3 tell`, no `ar3 dispatch`, no passthrough:
every action belongs to the CLI that owns it, and ar3's job is to tell you
which command that is. The one exception is `ar3 deps`, which fetches
on-demand heavy dependencies (boto3, textual) into `~/.local/share/ar3/deps`:
that directory is substrate ar3 itself owns, not product state, and it is the
only thing ar3 ever writes.

Home resolution imports the same `ar3.home.app_home` resolver the products
themselves call (A8S_HOME / R4T_HOME / K7E_HOME), so ar3's reporting can
never go stale against a product's own resolution.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# `ar3ver` sits at the repo root and carries the suite semver. A copy of this
# tree relocated away from that root (the isolation container copies apps/r4t
# alone to /opt/r4t) still has to run: the version is a nicety, never a
# dependency, so a missing module degrades to "unknown" instead of killing
# the CLI on import.
sys.path.append(str(Path(__file__).resolve().parents[2] / "lib"))
try:
    from ar3ver import update_note, version_line  # noqa: E402
except ImportError:
    def version_line(app: str) -> str:
        import platform

        return f"{app} unknown (ar3, python {platform.python_version()})"

    def update_note(timeout_s: float = 0) -> str:
        return "unknown (no VERSION file beside this copy)"

from ar3 import deps as ar3_deps  # noqa: E402
from ar3.home import app_home  # noqa: E402
from ar3.proc import pid_alive  # noqa: E402
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

WORDMARK = ("A R K", "8 4 7", "S T E")

TAGLINE = (
    "ar3 — a8s routes the messages, r4t governs the roster,",
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
    return pid if pid_alive(pid) else None


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
        rows.append((False, "rigs", f"no rig config at {rigs}", "r4t rig add <rig> <preset>"))
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
        None if rosters else "r4t add <dir> [<runbook>]",
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
    dir_ = ar3_deps.ensure_group(group)
    if dir_ is not None:
        return (True, group, f"installed at {dir_}", None)
    return (False, group, "not installed", f"ar3 deps {group}")


def cmd_deps(args: argparse.Namespace) -> int:
    group = getattr(args, "group", None)
    groups = ar3_deps.known_groups()
    if not group:
        interpreter_dir = ar3_deps.deps_root() / ar3_deps.interpreter_key()
        print(f"ar3 deps — on-demand heavy dependencies  ({interpreter_dir})")
        print()
        _print_rows([_deps_status_row(g) for g in groups])
        return 0
    if group not in groups:
        known = ", ".join(groups) if groups else "none defined"
        print(f"ar3 deps: no such group {group!r} (known: {known})", file=sys.stderr)
        return 2
    try:
        dest = ar3_deps.install_group(group)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"ar3 deps {group}: {e}", file=sys.stderr)
        return 1
    print(f"ar3 deps {group}: installed to {dest}")
    return 0


# ---------- update ----------

UPDATE_SCRIPT = "get.sh"


def _suite_version() -> str:
    """Read from disk each call, not through `ar3ver`'s import: this runs on
    both sides of an update that rewrites the file underneath us."""
    try:
        return (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _git_out(root: Path, *args: str) -> Optional[str]:
    """Trimmed stdout of a git command, or None when git failed or is absent.
    A non-zero exit is an answer here, not an error to report."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def update_refusal(root: Path) -> Optional[str]:
    """Why this tree must not be pulled forward, or None to proceed.

    `get.sh` reaches the installed tree with `git pull --ff-only` and, on a
    pinned version, `git checkout -f`. Against a checkout somebody is working
    in, that ranges from a confusing failure to discarded work, and no message
    printed afterwards undoes it. The two states that mean "someone is working
    here" therefore stop the update before it starts rather than after.

    A detached HEAD sitting exactly on a release tag is not one of them: that
    is what an `AR3_VERSION` pin leaves behind, and `get.sh` knows how to
    rejoin the branch from there. A detached HEAD anywhere else is a working
    state and is refused.

    Silence from git is a refusal, not a pass. Every clearance below is read
    out of git's own answers, so when git is missing, times out, or declines
    the directory as dubiously owned, this knows nothing about the tree. A
    disowned `.git` and an unreadable one are indistinguishable from here, and
    one of them is a working checkout, so a present-but-unreadable `.git`
    stops the update instead of clearing it.
    """
    if not (root / ".git").exists():
        return None
    if _git_out(root, "rev-parse", "--is-inside-work-tree") != "true":
        return (
            f"{root} holds a .git that git would not confirm as a work tree — "
            f"git may be missing, too slow, or refusing the directory as "
            f"dubiously owned. Those look identical from here, and one of them "
            f"is a checkout somebody is working in, so this stops rather than "
            f"assume the unreadable case is the safe one."
        )
    status = _git_out(root, "status", "--porcelain")
    if status is None:
        return (
            f"{root} is a git checkout whose status git would not report. "
            f"Updating pulls this tree forward, which is not something to do "
            f"without first knowing whether work is in progress here."
        )
    if status:
        return (
            f"{root} has uncommitted changes. Updating pulls this tree forward, "
            f"which is not something to do over work in progress — commit or "
            f"stash first, or point AR3_DIR at the install you meant."
        )
    branch = _git_out(root, "symbolic-ref", "--short", "-q", "HEAD")
    if branch is None:
        # `get.sh` accepts AR3_VERSION only as `v[0-9]*`, so that is the only
        # detached state it can have created. Any other tag — `wip`, a
        # release-candidate marker, someone's bookmark — is a working state
        # wearing a tag, and clearing it would let the installer force this
        # tree back onto the default branch.
        tag = _git_out(root, "describe", "--tags", "--exact-match")
        if not (tag and re.fullmatch(r"v[0-9].*", tag)):
            return (
                f"{root} is at a detached HEAD that is not an AR3_VERSION pin "
                f"(no tag matching {'v[0-9]*'!r}{f'; found {tag!r}' if tag else ''}). "
                f"That is a working state, and updating would force this tree "
                f"back onto the default branch. Check out a branch first, or "
                f"point AR3_DIR at the install you meant."
            )
        return None
    head = _git_out(root, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD")
    default = head.split("/", 1)[1] if head and "/" in head else "main"
    if branch != default:
        return (
            f"{root} is on branch {branch!r}, not {default!r}. This is a working "
            f"checkout, not an install — updating it would pull that branch "
            f"forward. Switch to {default!r} first, or point AR3_DIR at the "
            f"install you meant."
        )
    return None


def cmd_update(_args: argparse.Namespace) -> int:
    script = REPO_ROOT / UPDATE_SCRIPT
    if not script.is_file():
        print(
            f"ar3 update: no {UPDATE_SCRIPT} beside this copy ({REPO_ROOT}) — "
            f"reinstall from github.com/witw-llc/ar3 to get one",
            file=sys.stderr,
        )
        return 1
    refusal = update_refusal(REPO_ROOT)
    if refusal:
        print(f"ar3 update: {refusal}", file=sys.stderr)
        return 1
    before = _suite_version()
    # AR3_DIR is passed rather than left to default: `get.sh` alone would
    # update whatever lives at ~/.ar3, which is not necessarily the copy the
    # operator just invoked.
    env = {**os.environ, "AR3_DIR": str(REPO_ROOT)}
    try:
        done = subprocess.run(["sh", str(script)], env=env)
    except OSError as e:
        print(f"ar3 update: cannot run {script}: {e}", file=sys.stderr)
        return 1
    if done.returncode != 0:
        return done.returncode
    after = _suite_version()
    print()
    if before == after:
        print(f"ar3 update: already at {after}")
    else:
        print(f"ar3 update: {before} -> {after}")
    return 0


# ---------- cli ----------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ar3",
        description=(
            "Front door to the ar3 suite. Bare `ar3` reports where a8s, r4t and "
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
            "<group> installs that group into ~/.local/share/ar3/deps — the "
            "one thing ar3 ever writes."
        ),
    )
    deps.add_argument(
        "group", nargs="?",
        help="Dependency group to install, e.g. a8s-s3 or r4t (see requirements/*.txt)",
    )
    deps.set_defaults(func=cmd_deps)
    update = sub.add_parser(
        "update",
        help="Update this ar3 install in place",
        description=(
            "ar3 update runs the suite's own installer against the copy you "
            "invoked, which pulls it forward and restarts running a8s nodes so "
            "handlers re-exec the new code. AR3_VERSION pins a release and "
            "AR3_CHANNEL selects stable or beta, exactly as at install time. A "
            "working checkout — dirty, or on a branch other than the default — "
            "is refused rather than pulled."
        ),
    )
    update.set_defaults(func=cmd_update)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    # stderr already defaults to backslashreplace, so only stdout needs the
    # floor — an unencodable glyph (e.g. on a redirected Windows console)
    # gets a lossless, reversible escape instead of crashing the process.
    # The isinstance/errors=="strict" guard is mypy's own (PR 18292): it
    # never fires once a caller has set a deliberate error handler, and
    # skips a replaced sys.stdout (e.g. io.StringIO under embedding) cleanly
    # instead of raising AttributeError. Every --json path in the suite is
    # ensure_ascii, so machine-readable output is unaffected either way.
    if isinstance(sys.stdout, io.TextIOWrapper) and sys.stdout.errors == "strict":
        sys.stdout.reconfigure(errors="backslashreplace")
    sys.exit(main())
