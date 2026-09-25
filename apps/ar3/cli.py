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

# `ar3ver` and the `ar3` foundation package sit in `<repo>/lib` and carry the
# suite semver and the shared code. That directory goes to the FRONT of
# sys.path, never the end: appended, it loses to site-packages, and any
# unrelated distribution named `ar3` then answers these imports instead. A
# copy of this tree relocated away from the repo root (the isolation container
# copies apps/r4t alone to /opt/r4t) has no `lib` beside it; the version is a
# nicety, never a dependency, so a missing module degrades to "unknown"
# instead of killing the CLI on import.
_AR3_LIB = str(Path(__file__).resolve().parents[2] / "lib")
while _AR3_LIB in sys.path:
    sys.path.remove(_AR3_LIB)
sys.path.insert(0, _AR3_LIB)
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
from ar3.proc import pid_alive, process_start_token  # noqa: E402
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
IS_WINDOWS = os.name == "nt"

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
    """The pid in an a8s pid file when that process is still the one that
    claimed it. a8s stamps the claimer's start token in `pid.start` beside
    the file; a live pid whose start differs was reused by the OS and reads
    as stopped. No stamp, or no token on this platform, leaves liveness to
    decide. The same rule as a8s's own read, without its cleanup: the front
    door never changes another product's files."""
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if not pid_alive(pid):
        return None
    try:
        stamped = path.with_name("pid.start").read_text(encoding="utf-8").strip()
    except OSError:
        return pid
    current = process_start_token(pid) if stamped else None
    return None if current is not None and current != stamped else pid


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


def _utf8_report(os_name: str, utf8_mode: bool, io_encoding: str, stream: str) -> Probe:
    if os_name != "nt":
        return Probe(True, "not applicable — POSIX leaves the locale alone")
    stream = stream or "unknown"
    if utf8_mode:
        return Probe(True, f"UTF-8 mode on, stdout {stream}")
    if io_encoding:
        return Probe(True, f"PYTHONIOENCODING={io_encoding}, stdout {stream}")
    return Probe(False, f"UTF-8 mode off, stdout {stream}")


def _utf8_probe() -> Probe:
    """Whether the interpreter a launcher started runs in UTF-8 mode.

    This process is that child — `ar3` reached here through the same launcher
    every other verb does — so its own flags are the answer rather than a
    guess about one. A stock Windows console is cp1252, and the suite's output
    carries arrows, em dashes and agent names, so a child left on the console
    code page raises `UnicodeEncodeError` on a line it was only printing.

    POSIX has no equivalent failure to report: the launchers set nothing
    there, because a locale is the operator's own setting.

    The reading is split from the reporting because `sys.flags` cannot be
    assigned and `os.name` cannot be changed without lying to every other
    module in the process, so the branch table has no other way to be tested
    from the platform that does not have the problem.
    """
    return _utf8_report(
        os.name,
        bool(sys.flags.utf8_mode),
        os.environ.get("PYTHONIOENCODING", ""),
        getattr(sys.stdout, "encoding", ""),
    )


CHECKS: tuple[Check, ...] = (
    Check("claude", HARNESS, _version_probe("claude"), "install Claude Code"),
    Check("agent", HARNESS, _version_probe("agent"), "install the Cursor agent CLI"),
    Check("codex", HARNESS, _version_probe("codex"), "install the Codex CLI"),
    Check("copilot", HARNESS, _version_probe("copilot"), "install the GitHub Copilot CLI"),
    Check("opencode", HARNESS, _version_probe("opencode"), "install OpenCode"),
    Check("agy", HARNESS, _version_probe("agy"), "install Antigravity"),
    Check("muse", HARNESS, _version_probe("muse"), "install Meta Muse"),
    Check("devin", HARNESS, _version_probe("devin"), "install the Devin CLI"),
    Check("ollama", HARNESS, _version_probe("ollama"), "install ollama"),
    Check("ollama serve", SERVICES, _ollama_probe, "ollama serve, then ollama pull <model>"),
    Check("docker", SERVICES, _docker_probe, "start Docker Desktop or the docker daemon"),
    Check("git", TOOLING, _git_probe, "git config --global user.name / user.email", core=True),
    Check(
        "utf-8 output",
        TOOLING,
        _utf8_probe,
        "set PYTHONUTF8=1, or run the suite through its own launchers, which set it",
    ),
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


# ---------- engine updates ----------

# The binary each HARNESS check probes is also the engine's update target.
# Self-update argv (plus env overlay) for the CLIs that bring themselves
# current; engines absent here — codex, copilot, ollama — have no such verb
# and only update through their package manager.
ENGINE_SELF_UPDATE: dict[str, tuple[tuple[str, ...], dict[str, str]]] = {
    "claude": (("claude", "update"), {}),
    "agent": (("agent", "update"), {}),
    "agy": (("agy", "update"), {}),
    # muse has no update verb; its launcher updates itself on any invocation
    # when MUSE_SYNC_UPDATE=1, otherwise on a once-an-hour timer.
    "muse": (("muse", "--version"), {"MUSE_SYNC_UPDATE": "1"}),
    "devin": (("devin", "update"), {}),
    "opencode": (("opencode", "upgrade"), {}),
}

# --engine spellings that name an engine rather than its binary.
ENGINE_ALIASES = {"cursor": "agent"}


def engine_binaries() -> list[str]:
    """The updateable engine set — the same names `ar3 doctor` probes."""
    return [c.name for c in CHECKS if c.group == HARNESS]


def _npm_bin_owner(node_modules: Path, stem: str) -> str | None:
    """The package under `node_modules` whose bin map publishes `stem` —
    how a wrapper script like `<prefix>/codex.cmd` names the package it
    execs, without following the script's contents."""
    candidates = sorted(node_modules.glob("*/package.json"))
    candidates += sorted(node_modules.glob("@*/*/package.json"))
    for pkg_json in candidates:
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        binfield = data.get("bin")
        if isinstance(binfield, dict):
            names = binfield
        elif isinstance(binfield, str):
            names = [str(data.get("name", "")).rsplit("/", 1)[-1]]
        else:
            continue
        if stem in names:
            name = data.get("name")
            if isinstance(name, str):
                return name
    return None


def _npm_node_modules(binary: Path) -> Path | None:
    """The global `node_modules` a resolved binary belongs to — reached
    through the path itself for a real package file, or beside the wrapper
    scripts npm drops at the prefix (a `foo.cmd` on Windows has no symlink
    chain to follow)."""
    parts = binary.parts
    if "node_modules" in parts:
        return Path(*parts[: parts.index("node_modules") + 1])
    for candidate in (
        binary.parent / "node_modules",
        binary.parent.parent / "node_modules",
        binary.parent.parent / "lib" / "node_modules",
    ):
        if candidate.is_dir():
            return candidate
    return None


def install_method(binary: str) -> tuple[str | None, str | None, Path | None]:
    """(manager, package, node_modules) for a resolved binary path, or
    (None, None, None) when the path answers none — a brew Cellar or
    Caskroom install, or an npm global, each identified by where the
    binary lands. The third element is the npm root for globals and None
    otherwise."""
    resolved = Path(binary).resolve()
    parts = resolved.parts
    for marker, manager in (
        ("Cellar", "brew"),
        ("Caskroom", "brew-cask"),
    ):
        if marker not in parts:
            continue
        i = parts.index(marker)
        if i + 1 >= len(parts):
            continue
        return manager, parts[i + 1], None
    node_modules = _npm_node_modules(resolved)
    if node_modules is None:
        return None, None, None
    if "node_modules" in parts:
        i = parts.index("node_modules")
        package = parts[i + 1] if i + 1 < len(parts) else None
        if package is not None and package.startswith("@"):
            package = (
                f"{package}/{parts[i + 2]}" if i + 2 < len(parts) else None
            )
        if package == ".bin":  # the shim directory, not a package
            package = _npm_bin_owner(node_modules, resolved.stem)
    else:
        package = _npm_bin_owner(node_modules, resolved.stem)
    if package is None:
        return None, None, None
    return "npm", package, node_modules


def engine_update(name: str) -> tuple[list[str] | None, dict[str, str], str | None]:
    """(argv, env overlay, refusal) — how to bring engine `name` current.

    A package-managed install is updated by its manager, not the engine's own
    verb: a self-updater would lay a second copy beside the managed one.
    Every command name is resolved through `which` before it is returned —
    Windows' CreateProcess only appends `.exe` to a bare name, so an
    unresolved `npm` or `agent` would fail on the `.cmd` shims npm and the
    installers ship there. npm installs whose global root the caller cannot
    write go through sudo — the command asks for the password itself, and a
    platform without sudo gets a refusal instead. brew is never run under
    sudo; it refuses, so a root-owned Cellar fails with brew's own error."""
    binary = shutil.which(name)
    if binary is None:
        return None, {}, f"{name} is not on PATH"
    manager, package, node_modules = install_method(binary)
    if manager in ("brew", "brew-cask"):
        tool = shutil.which("brew")
        if tool is None:
            return None, {}, (
                f"{name} is a Homebrew install ({package}) but brew is not "
                f"on PATH — update it the way it was installed"
            )
        argv = [tool, "upgrade", package]
        if manager == "brew-cask":
            argv.insert(2, "--cask")
        return argv, {}, None
    if manager == "npm":
        tool = shutil.which("npm")
        if tool is None:
            return None, {}, (
                f"{name} is an npm global ({package}) but npm is not on "
                f"PATH — update it the way it was installed"
            )
        # `--prefix` pins the install this binary came from: the first npm
        # on PATH can belong to a different global root (a Node version
        # manager's), which would install a second copy and leave the
        # detected one stale. npm puts globals under <prefix>/lib on POSIX
        # and directly at <prefix> on Windows.
        prefix = (
            node_modules.parent.parent
            if node_modules.parent.name == "lib"
            else node_modules.parent
        )
        argv = [
            tool,
            "install",
            "-g",
            "--prefix",
            str(prefix),
            f"{package}@latest",
        ]
        if not os.access(node_modules, os.W_OK):
            sudo = shutil.which("sudo")
            if sudo is None:
                return None, {}, (
                    f"{name} is installed under {node_modules}, which you "
                    f"cannot write — update it as that directory's owner"
                )
            argv = [sudo, *argv]
        return argv, {}, None
    self_update = ENGINE_SELF_UPDATE.get(name)
    if self_update is not None:
        argv, env = self_update
        argv = [shutil.which(argv[0]) or argv[0], *argv[1:]]
        return argv, dict(env), None
    return None, {}, (
        f"{name} has no known update method — update it the way it was installed"
    )


def _engine_names(args: argparse.Namespace) -> tuple[list[str] | None, int]:
    """The binary names the flags select, or (None, exit code) on a usage
    error — resolved before any node is stopped so a bad flag cannot cycle
    the fleet."""
    if args.all_engines:
        names = [n for n in engine_binaries() if shutil.which(n)]
        if not names:
            print("ar3 update: no engines found on PATH", file=sys.stderr)
            return None, 1
        return names, 0
    names = []
    for raw in args.engines:
        name = ENGINE_ALIASES.get(raw.strip().lower(), raw.strip().lower())
        if name.startswith("ollama-"):
            name = "ollama"
        if name not in engine_binaries():
            print(
                f"ar3 update: unknown engine {raw!r} "
                f"(engines: {', '.join(engine_binaries())})",
                file=sys.stderr,
            )
            return None, 2
        if name not in names:
            names.append(name)
    return names, 0


def _run_engine_updates(names: list[str]) -> int:
    failures = 0
    for name in names:
        argv, env, refusal = engine_update(name)
        if argv is None:
            print(f"ar3 update: {name}: {refusal}", file=sys.stderr)
            failures += 1
            continue
        print(f"ar3 update: {name}: {' '.join(argv)}")
        try:
            done = subprocess.run(argv, env={**os.environ, **env})
        except OSError as e:
            print(f"ar3 update: {name}: cannot run {argv[0]}: {e}", file=sys.stderr)
            failures += 1
            continue
        if done.returncode != 0:
            print(
                f"ar3 update: {name}: update failed (exit {done.returncode})",
                file=sys.stderr,
            )
            failures += 1
    return 1 if failures else 0


def _a8s() -> Optional[str]:
    """The a8s shim — on PATH (as install.sh leaves it) or beside this copy.
    None means nodes cannot be stopped or started from here."""
    found = shutil.which("a8s")
    if found:
        return found
    shim = REPO_ROOT / "a8s"
    return str(shim) if shim.is_file() else None


def _running_groups(a8s: str) -> dict[int, list[str]] | None:
    """Handler PID → node names it serves, from `a8s ps` — or None when ps
    cannot answer (a8s missing, errored, or slow). None is not "nothing
    running": a caller that would restart the difference must not guess,
    or it could start a second handler beside a live one. The PID grouping
    matters because one handler serves a whole alias's members under a
    single remote session identity."""
    try:
        done = subprocess.run(
            [a8s, "ps"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    groups: dict[int, list[str]] = {}
    for line in (done.stdout or "").splitlines():
        cols = line.split()
        if len(cols) < 3 or not cols[1].isdigit():
            continue
        groups.setdefault(int(cols[1]), []).append(cols[0])
    return groups


def _running_nodes(a8s: str) -> list[str] | None:
    groups = _running_groups(a8s)
    if groups is None:
        return None
    return [n for names in groups.values() for n in names]


def _a8s_names() -> tuple[set[str], dict[str, list[str]]]:
    """(agent names, alias→members) from a8s's own registry file — the
    data `a8s start <alias>` resolves against. Missing or malformed reads
    as empty: no alias matches, and groups fall back to per-node starts."""
    try:
        data = json.loads((a8s_home() / "a8s.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), {}
    if not isinstance(data, dict):
        return set(), {}
    agents = data.get("agents")
    aliases = data.get("aliases")
    return (
        set(agents) if isinstance(agents, dict) else set(),
        aliases if isinstance(aliases, dict) else {},
    )


def _expand_alias(
    agents: set[str], aliases: dict[str, list[str]], name: str
) -> list[str] | None:
    """Resolved member names of alias `name`, or None when a8s itself
    would refuse it (cycle, unknown member). Mirrors `resolve_name` in
    apps/a8s/registry.py: a diamond is not a cycle, and agent names dedup
    by canonical case."""
    agent_lookup = {a.lower(): a for a in agents}
    alias_lookup = {a.lower(): a for a in aliases}
    out: list[str] = []
    path: set[str] = set()
    seen: set[str] = set()

    def walk(member: object) -> None:
        key = str(member).lower()
        if key in agent_lookup:
            resolved = agent_lookup[key]
            if resolved not in out:
                out.append(resolved)
            return
        if key in alias_lookup:
            if key in path:
                raise ValueError(f"alias cycle at {member!r}")
            if key in seen:
                return
            members = aliases[alias_lookup[key]]
            if not isinstance(members, list):
                raise KeyError(member)
            path.add(key)
            try:
                for m in members:
                    walk(m)
            finally:
                path.discard(key)
            seen.add(key)
            return
        raise KeyError(member)

    try:
        walk(name)
    except (KeyError, ValueError):
        return None
    return out


def _restart_targets(
    groups: dict[int, list[str]],
    stopped: list[str],
    agents: set[str],
    aliases: dict[str, list[str]],
) -> list[str]:
    """One `a8s start` target per handler that went down, mirroring
    `_update_restart_targets` in apps/a8s/commands.py: a group that
    stopped together restarts through the alias resolving to exactly its
    members — one process, the same `a,b` node tag, and the remote
    session identity it feeds — otherwise each member starts alone.
    Only nodes confirmed down become targets; a node still running must
    not get a second handler."""
    down = {n.lower() for n in stopped}
    targets: list[str] = []
    for _pid, names in sorted(groups.items(), key=lambda kv: kv[1][0].lower()):
        group_down = [n for n in names if n.lower() in down]
        if not group_down:
            continue
        name_set = {n.lower() for n in names}
        hit = None
        if len(group_down) == len(names):
            for alias in sorted(aliases, key=str.lower):
                members = _expand_alias(agents, aliases, alias)
                if members is not None and {
                    m.lower() for m in members
                } == name_set:
                    hit = alias
                    break
        if hit is not None:
            targets.append(hit)
        else:
            targets.extend(group_down)
    return targets


def _stop_nodes(a8s: str, names: list[str], stopped: list[str]) -> None:
    """`a8s stop` each running node — it waits for the current wake to
    detach, so nothing is mid-turn when its engine binary is replaced.
    Every originally-running name confirmed down is appended to `stopped`
    as it happens: one handler can serve several nodes, so a single stop
    can take siblings with it, and a loop interrupted mid-stop still
    leaves `stopped` holding what the restart must bring back. A node
    that will not stop is reported and the update continues rather than
    held hostage."""
    live = list(names)
    for name in names:
        if name not in live:
            continue
        print(f"ar3 update: stopping node {name}")
        try:
            done = subprocess.run([a8s, "stop", name])
        except OSError as e:
            print(f"ar3 update: {name}: cannot stop: {e}", file=sys.stderr)
            continue
        if done.returncode != 0:
            print(
                f"ar3 update: {name}: stop exited {done.returncode} — "
                f"its engine may be mid-turn while it is replaced",
                file=sys.stderr,
            )
            continue
        refreshed = _running_nodes(a8s)
        down = (
            [name]
            if refreshed is None
            else [n for n in names if n not in refreshed]
        )
        stopped.extend(n for n in down if n not in stopped)
        if refreshed is not None:
            live = refreshed


def _start_nodes(a8s: str, names: list[str]) -> int:
    """Bring back exactly the nodes `_stop_nodes` recorded — ones that
    were already down stay down. A failed start is reported and counted
    so the update's exit status reflects a node left offline; the rest
    still try."""
    failures = 0
    for name in names:
        print(f"ar3 update: starting node {name}")
        try:
            done = subprocess.run([a8s, "start", name])
        except OSError as e:
            print(f"ar3 update: {name}: cannot start: {e}", file=sys.stderr)
            failures += 1
            continue
        if done.returncode != 0:
            print(
                f"ar3 update: {name}: start exited {done.returncode} — "
                f"start it yourself with `a8s start {name}`",
                file=sys.stderr,
            )
            failures += 1
    return 1 if failures else 0


def _posix_sh() -> Optional[str]:
    """The `sh` that runs get.sh, or None when nothing here can run it.

    POSIX has one on every PATH. Windows does not: Git for Windows puts its
    `cmd\\` directory on the user Path and keeps `sh.exe` under `usr\\bin`,
    and the `bash.exe` System32 does hold is the WSL launcher, which would
    run the installer inside a Linux distro against a Windows path. So when
    PATH has no `sh`, the shell is read off git's own install tree, the way
    git itself names it. `bin\\sh.exe` comes first: it is the launcher that
    puts `/usr/bin` (uname, cygpath) on the child's PATH the way Git Bash
    does, where `usr\\bin\\sh.exe` is the bare interpreter with only the
    caller's PATH.
    """
    found = shutil.which("sh")
    if found or not IS_WINDOWS:
        return found
    exec_path = _git_out(REPO_ROOT, "--exec-path")
    if not exec_path:
        return None
    parents = Path(exec_path).parents  # <root>/mingw64/libexec/git-core
    if len(parents) < 3:
        return None
    root = parents[2]
    for candidate in (root / "bin" / "sh.exe", root / "usr" / "bin" / "sh.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


def _update_suite(script: Path) -> int:
    """get.sh against this copy — the pre-flag behavior of `ar3 update`."""
    sh = _posix_sh()
    if sh is None:
        print(
            f"ar3 update: no sh to run {script.name} with — on Windows, install "
            f"Git for Windows (its sh runs the installer) or run this from Git Bash",
            file=sys.stderr,
        )
        return 1
    before = _suite_version()
    # AR3_DIR is passed rather than left to default: `get.sh` alone would
    # update whatever lives at ~/.ar3, which is not necessarily the copy the
    # operator just invoked.
    env = {**os.environ, "AR3_DIR": str(REPO_ROOT)}
    try:
        done = subprocess.run([sh, str(script)], env=env)
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


def _update_everything(args: argparse.Namespace, script: Path) -> int:
    """The agent-machine pipeline: stop running nodes so no handler is
    mid-turn on a binary about to be replaced, update the engines, update
    the suite, then start exactly the nodes that were stopped. Nodes come
    back in a `finally` — a failed update must not leave a machine dark."""
    names, err = _engine_names(args)
    if names is None:
        return err
    stopped: list[str] = []
    running: list[str] = []
    groups: dict[int, list[str]] = {}
    a8s = _a8s()
    restart_rc = 0
    try:
        if a8s is None:
            print(
                "ar3 update: a8s not found — running nodes cannot be stopped "
                "before their engines are replaced; continuing anyway",
                file=sys.stderr,
            )
        else:
            groups = _running_groups(a8s) or {}
            running = [n for names in groups.values() for n in names]
            _stop_nodes(a8s, running, stopped)
        engine_rc = _run_engine_updates(names)
        suite_rc = _update_suite(script)
    finally:
        if a8s is not None:
            # An interrupted stop may have taken a node down without a
            # recorded entry — reconcile against what is still up, but only
            # when ps answers: guessing here could start a second handler
            # beside a live one.
            live = _running_nodes(a8s)
            if live is not None:
                stopped.extend(
                    n
                    for n in running
                    if n not in stopped and n not in live
                )
            agents, aliases = _a8s_names()
            targets = _restart_targets(groups, stopped, agents, aliases)
            restart_rc = _start_nodes(a8s, targets)
    return suite_rc or engine_rc or restart_rc


def cmd_update(args: argparse.Namespace) -> int:
    if args.all_engines and args.engines:
        print(
            "ar3 update: --engine and --all-engines contradict — pick one",
            file=sys.stderr,
        )
        return 2
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
    if args.all_engines or args.engines:
        return _update_everything(args, script)
    return _update_suite(script)


# ---------- cli ----------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ar3",
        description=(
            "Front door to the AR3 suite. Bare `ar3` reports where a8s, r4t and "
            "k7e stand; `ar3 doctor` probes the tools they need. AR3 never runs "
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
            "one thing AR3 ever writes."
        ),
    )
    deps.add_argument(
        "group", nargs="?",
        help="Dependency group to install, e.g. a8s-s3 or r4t (see requirements/*.txt)",
    )
    deps.set_defaults(func=cmd_deps)
    update = sub.add_parser(
        "update",
        help="Update this AR3 install in place, or the engine binaries",
        description=(
            "ar3 update runs the suite's own installer against the copy you "
            "invoked, which pulls it forward and restarts running a8s nodes so "
            "handlers re-exec the new code. AR3_VERSION pins a release and "
            "AR3_CHANNEL selects stable or beta, exactly as at install time. A "
            "working checkout — dirty, or on a branch other than the default — "
            "is refused rather than pulled. With --engine or --all-engines it "
            "also updates engine binaries: running a8s nodes are stopped "
            "first so nothing is mid-turn while a binary is replaced, the "
            "engines update, the suite update runs, and the stopped nodes "
            "start again — the whole agent-machine refresh in one line."
        ),
    )
    update.add_argument(
        "--engine",
        nargs="+",
        metavar="NAME",
        dest="engines",
        help="Also update the named engine(s), each by its own method — a "
        "self-update verb, brew, or npm (which may prompt for sudo). Known: "
        f"{', '.join(engine_binaries())}; engine ids like cursor or "
        "ollama-codex are accepted too.",
    )
    update.add_argument(
        "--all-engines",
        action="store_true",
        dest="all_engines",
        help="Also update every engine binary found on PATH — the set "
        "`ar3 doctor` probes — each by its own method.",
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
