"""`a8s health <node>` — one inspection of a local node: its definition, the
variables that shape it, runtime and activity, the probes a8s already has,
and the engine's live quota.

The inspection reaps no pid file, repairs no mailbox, consumes no inbox item,
restarts nothing and keeps no snapshot. Credentials used by probes are never
displayed. A fact it cannot read is reported as unknown or
unavailable, never as zero or idle. Quota is asked of the sibling r4t as a
bounded child under the node's composed environment; only a live answer is
shown.
"""
from __future__ import annotations

import os
import re
import select
import subprocess
import stat
import sys
import tempfile
import json
import math
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import txlog
from ar3 import clock
from ar3.proc import find_executable, launch_options, terminate_group
from commands import (
    _ENGINE_CHECK_TIMEOUT_SECONDS,
    _pid_uptime,
    _probe_remotes,
    _r4t_engine_id,
)
from core import (
    SCRIPT_DIR,
    dead_letters_dir,
    inbox_dir,
    last_wake_path,
    pid_path,
    read_wake_pid,
)
from daemon import read_handler_pid_strict, run_status_probe
from definitions import (
    IS_WINDOWS,
    PLACEHOLDER_RE,
    argv_vars,
    default_definition_path,
    definition_env,
    definition_stem,
    _MAX_WRAPPER_DEPTH,
    _SHELLS,
    _WRAPPERS,
    harness_program,
    load_agent_env,
    load_definition,
    split_env_var_key,
    wake_env,
    wake_shell,
)
from registry import resolve_name, resolve_recipient, unresolved_mailboxes
from txlog import last_heard

R4T_PY = SCRIPT_DIR.parent / "r4t" / "r4t.py"

# Covers the slowest adapter (a local-API engine, 45 s) with room for start-up.
QUOTA_TIMEOUT_SECONDS = 60
CHECK_TIMEOUT_SECONDS = _ENGINE_CHECK_TIMEOUT_SECONDS
# After a timeout the child's group gets SIGTERM and this long to run its own
# cleanup (r4t releases what it holds on SIGTERM) before SIGKILL.
QUOTA_TEARDOWN_GRACE_SECONDS = 3.0
CHECK_TEARDOWN_GRACE_SECONDS = 0.5
# After a normal exit, stragglers in the child's group get this long.
SETTLE_SECONDS = 0.25
OUTPUT_LIMIT = 1 << 20
SNAPSHOT_AGE_LIMIT_SECONDS = 300

# The engine id r4t answers for, by the program a native (non-r4t) definition
# runs. Engine ids and program names differ for cursor (`agent`, or the older
# `cursor-agent`); the table is the r4t preset mapping, not a filename guess.
NATIVE_PROGRAMS = {
    "claude": "claude",
    "codex": "codex",
    "copilot": "copilot",
    "agy": "agy",
    "muse": "muse",
    "devin": "devin",
    "opencode": "opencode",
    "agent": "cursor",
    "cursor-agent": "cursor",
}
# `ollama launch <cli>` is r4t's `ollama-<cli>` engine.
OLLAMA_LAUNCHED = frozenset({"claude", "codex", "opencode", "copilot"})
# The binary r4t itself starts for an engine, where it is not the engine id.
ENGINE_BINARY = {
    "cursor": "agent",
    "ollama-claude": "ollama",
    "ollama-codex": "ollama",
    "ollama-opencode": "ollama",
    "ollama-copilot": "ollama",
}

# Launch knobs whose values are not secret. Every other var shows its key only.
SHOWN_KNOBS = ("MODEL", "EFFORT", "TIMEOUT", "MEMORY")

# Variables that pick which account an engine reads.
ACCOUNT_ENV_NAMES = (
    "HOME", "USERPROFILE", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
    "COPILOT_HOME", "CLAUDE_CODE_OAUTH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN",
    "COPILOT_GITHUB_TOKEN",
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def _clean(value: object, secrets: tuple[str, ...] = (), limit: int = 300) -> str:
    """One printable line: control characters collapsed, any known secret
    value masked, and a length cap."""
    text = _CONTROL.sub(" ", str(value)).strip()
    for secret in secrets:
        text = text.replace(secret, "***")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _err(message: str) -> None:
    print(f"a8s: health: {message}", file=sys.stderr)


# ---------- bounded child ----------

KQUEUE_SLICE_SECONDS = 0.1


def _kqueue_exit(proc: subprocess.Popen, seconds: float) -> bool:
    """True once the leader has exited, observed with kqueue NOTE_EXIT, which
    does not reap. The filter is registered again every slice: a zombie refused
    with ESRCH has exited, and an exit that fell between lookup and attach is
    caught by the next registration. Nothing else may reap the child (SIGCHLD
    not ignored, no `waitpid(-1)`)."""
    deadline = time.monotonic() + max(0.0, seconds)
    kq = select.kqueue()
    try:
        event = select.kevent(
            proc.pid, filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT,
        )
        while True:
            try:
                kq.control([event], 0, 0)
            except ProcessLookupError:
                return True
            wait = max(0.0, min(KQUEUE_SLICE_SECONDS, deadline - time.monotonic()))
            for got in kq.control(None, 1, wait):
                if got.ident == proc.pid and got.fflags & select.KQ_NOTE_EXIT \
                        and not got.flags & select.KQ_EV_ERROR:
                    return True
            if time.monotonic() >= deadline:
                return False
    finally:
        kq.close()


def _observe_leader(proc: subprocess.Popen, seconds: float) -> str:
    """Wait at most `seconds` for the group leader: `exited` (finished, still
    unreaped, so its pid and group id stay reserved), `reaped` (finished and
    gone: nothing may be signalled by its pid) or `running`. `waitid` with
    WNOWAIT where it exists (Linux, macOS on Python 3.13+), else kqueue
    (older macOS), else a reaping poll."""
    if proc.returncode is not None:
        return "reaped"
    deadline = time.monotonic() + max(0.0, seconds)
    if hasattr(os, "waitid"):
        while True:
            try:
                if os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                    return "exited"
            except ChildProcessError:
                return "reaped"
            if time.monotonic() >= deadline:
                return "running"
            time.sleep(0.02)
    if hasattr(select, "kqueue"):
        return "exited" if _kqueue_exit(proc, seconds) else "running"
    while True:
        if proc.poll() is not None:
            return "reaped"
        if time.monotonic() >= deadline:
            return "running"
        time.sleep(0.02)


def _leader_exited(proc: subprocess.Popen) -> bool:
    return _observe_leader(proc, 0) != "running"


def _await_leader(proc: subprocess.Popen, seconds: float) -> bool:
    """True if the leader finished within `seconds`; never blocks longer."""
    return _observe_leader(proc, seconds) != "running"


def _signal_group(pgid: int, sig: int) -> None:
    """Signal a process group by its id only. A bare pid is never signalled: a
    pid can be reused once the leader is reaped, a group id cannot while any
    member lives."""
    try:
        os.killpg(pgid, 0)
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _end_group(proc: subprocess.Popen, *, grace: float) -> None:
    """End everything the child started. POSIX: SIGTERM the group while the
    leader is still unreaped, wait up to `grace` for the leader to finish its
    cleanup, give the rest a moment, then SIGKILL the group. Every wait is
    bounded. A leader already reaped is never signalled by its pid. Windows has no group: its tree is ended only when the leader is
    still running, never after a normal exit, when its pid may be reused."""
    if os.name != "posix":
        if not _leader_exited(proc):
            terminate_group(proc)
        return
    state = _observe_leader(proc, 0)
    if state == "reaped":
        return
    exited = state == "exited"
    _signal_group(proc.pid, signal.SIGTERM)
    if not exited:
        _await_leader(proc, grace)
    time.sleep(SETTLE_SECONDS)
    _signal_group(proc.pid, signal.SIGKILL)


def _abandon(proc: subprocess.Popen, grace: float) -> None:
    """Teardown while an interrupt, exit request or unexpected error is
    already unwinding the wait. The child runs in its own session, so no
    terminal signal reaches it. Whatever happens here, the original exception
    is the one the caller sees. A second interrupt during the graceful pass
    falls back to an immediate kill. The same no-signal-after-reap rule as
    `_end_group` holds: a leader already reaped is never signalled."""
    try:
        _end_group(proc, grace=grace)
        proc.wait(timeout=2)
    except BaseException:
        try:
            if os.name == "posix":
                if _observe_leader(proc, 0) != "reaped":
                    _signal_group(proc.pid, signal.SIGKILL)
            elif not _leader_exited(proc):
                terminate_group(proc)
            proc.wait(timeout=2)
        except BaseException:
            pass


def _run_bounded(
    argv: list[str], env: dict[str, str], cwd: Path, timeout: float,
    grace: float = CHECK_TEARDOWN_GRACE_SECONDS,
) -> tuple[int | None, str, str | None]:
    """`(returncode, stdout, problem)` for a child run in its own process
    group. Output goes to an unlinked temp file, never a pipe: a descendant
    that outlives the leader holds no pipe a reader could hang on. The group
    is ended after the leader finishes or on timeout (see `_end_group`), so
    nothing the child left behind survives. stderr is discarded; it is
    provider text."""
    with tempfile.TemporaryFile() as sink:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.DEVNULL,
                **launch_options(new_session=True, windows=IS_WINDOWS),
            )
        except OSError:
            return None, "", "r4t could not be started"
        try:
            if os.name == "posix":
                finished = _await_leader(proc, timeout)
            else:
                try:
                    proc.wait(timeout=timeout)
                    finished = True
                except subprocess.TimeoutExpired:
                    finished = False
            _end_group(proc, grace=grace)
            try:
                code = proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                code = None
        except BaseException:
            _abandon(proc, grace)
            raise
        returncode = code if finished else None
        problem = None if finished else f"timed out after {timeout:g}s"
        sink.seek(0)
        data = sink.read(OUTPUT_LIMIT)
    return returncode, data.decode("utf-8", errors="replace"), problem


# ---------- engine detection ----------

class Engine:
    """What the definition's own invoke lines say about the node's engine.

    kind: `engine` (id known and pinned), `none`, `roster`, `ambiguous`,
    `unpinned` (an id, but the binary the node runs is not the one r4t would
    query), `unproven` (a line reaches the engine through a wrapper or shell
    that can change its environment). `lines` are the (label, argv) pairs that name the engine."""

    def __init__(self, kind: str, engine: str | None = None, detail: str = "",
                 lines: list[tuple[str, list[str]]] | None = None):
        self.kind = kind
        self.engine = engine
        self.detail = detail
        self.lines = lines or []


def _invoke_lines(definition: dict) -> list[tuple[str, list[str]]]:
    lines = []
    sources = (
        ("invoke", definition.get("invoke")),
        ("idle.invoke", (definition.get("idle") or {}).get("invoke")
         if isinstance(definition.get("idle"), dict) else None),
        ("batch.invoke", (definition.get("batch") or {}).get("invoke")
         if isinstance(definition.get("batch"), dict) else None),
    )
    for label, argv in sources:
        if isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv):
            lines.append((label, argv))
    return lines


def _roster_line(argv: list[str]) -> bool:
    for i, token in enumerate(argv[:-1]):
        if token.endswith("r4t.py") and argv[i + 1] in ("dispatch", "idle"):
            return True
    return False


def _native_name(program: str) -> str:
    name = Path(program.replace("\\", "/")).name.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _native_engine(argv: list[str], program: str) -> str | None:
    name = _native_name(program)
    if name == "ollama":
        rest = argv[argv.index(program) + 1:]
        if len(rest) >= 2 and rest[0] == "launch" and rest[1] in OLLAMA_LAUNCHED:
            return f"ollama-{rest[1]}"
        return None
    return NATIVE_PROGRAMS.get(name)


def _same_binary(program: str, engine: str, env: dict[str, str], cwd: Path) -> str | None:
    """None when the program the node runs is the file r4t would start for
    `engine`; otherwise why not."""
    wanted_name = ENGINE_BINARY.get(engine, engine)
    slashed = os.sep in program or "/" in program
    if not slashed and _native_name(program) == wanted_name:
        return None
    path = env.get("PATH")
    ours = (os.path.join(str(cwd), program) if slashed
            else find_executable(program, path, cwd))
    theirs = find_executable(wanted_name, path, cwd)
    if ours is None or theirs is None or os.path.realpath(ours) != os.path.realpath(theirs):
        return f"r4t would query {wanted_name!r} from PATH ({theirs or 'nothing'})"
    return None


# Wrappers that run the command unchanged: they alter scheduling, locking or
# time limits, preserving the account environment, user and working folder.
_NEUTRAL_WRAPPERS = frozenset({"nohup", "setsid", "time", "nice", "ionice", "timeout", "flock"})


def _launch_prefix(argv: list[str]) -> tuple[str | None, int | None]:
    """`(blocker, index)`: walk the wrappers at the head of `argv` the way
    `harness_program` does. `index` is where the first non-wrapper token sits.
    `blocker` is the base name of the first token that can change the launch
    context (an environment, user or folder wrapper, or a shell), or a fixed
    word when the chain is too deep to follow; None when every wrapper passed
    is neutral. Only the program's name is ever returned, never an operand."""
    i = 0
    for _ in range(_MAX_WRAPPER_DEPTH):
        if i >= len(argv):
            return None, None
        base = Path(argv[i].replace("\\", "/")).name
        if base in _SHELLS:
            return base, None
        entry = _WRAPPERS.get(base)
        if entry is None:
            return None, i
        if base not in _NEUTRAL_WRAPPERS:
            return base, None
        value_flags, operands = entry
        i += 1
        while i < len(argv) and argv[i].startswith("-"):
            takes_value = argv[i] in value_flags and "=" not in argv[i]
            i += 1
            if takes_value and i < len(argv):
                i += 1
        i += operands
    return "nested wrappers", None


def _r4t_script_index(argv: list[str]) -> int | None:
    for i, tok in enumerate(argv):
        if tok.endswith("r4t.py") and i + 3 < len(argv) \
                and argv[i + 1] == "engine" and argv[i + 3] == "run":
            return i
    return None


def _launch_unproven(argv: list[str], script_at: int | None) -> str | None:
    """Why the context this line launches in cannot be established, or None.
    A health child gets the node's composed environment, so a line that
    changes it on the way to the engine would be queried for the wrong
    account. Such lines are refused, never emulated. A line that starts r4t
    must reach `r4t.py` through at most neutral wrappers, one interpreter and
    its flags."""
    blocker, index = _launch_prefix(argv)
    if blocker is not None:
        return f"it starts the engine through {_clean(blocker, limit=40)!r}"
    if script_at is not None and index != script_at:
        if index is None or index > script_at:
            return "it reaches r4t through an unsupported prefix"
        interpreter = _native_name(argv[index])
        python = argv[index] == "$PYTHON" or re.fullmatch(r"python(?:[23](?:\.\d+)?)?|py", interpreter)
        flags = argv[index + 1:script_at]
        allowed = {"-u", "-B", "-E", "-I", "-s", "-S", "-O", "-OO", "-b", "-bb", "--"}
        if not python or any(flag not in allowed and not (
                interpreter == "py" and re.fullmatch(r"-[23](?:\.\d+)?", flag)
        ) for flag in flags):
            return "it reaches r4t through an unsupported prefix"
    return None


def _detect_engine(definition: dict, env: dict[str, str], cwd: Path | None) -> Engine:
    """Read the actual invoke, idle.invoke and batch.invoke lines. Lines that
    name different engines, or a roster beside an engine, are ambiguous: no
    one engine is invented for them. Every engine line is validated first, so
    one plain line cannot vouch for another that changes its environment."""
    found: dict[str, tuple[str, list[str], str | None]] = {}
    unproven: dict[str, tuple[str, str]] = {}
    roster = False
    opaque: dict[str, str] = {}
    for label, argv in _invoke_lines(definition):
        engine_id = _r4t_engine_id(argv)
        blocker, _ = _launch_prefix(argv)
        if blocker is not None:
            unproven[label] = (engine_id.lower() if engine_id else "",
                               f"it uses unsupported wrapper {blocker!r}")
            continue
        if engine_id is not None:
            why = _launch_unproven(argv, _r4t_script_index(argv))
            if why:
                unproven[label] = (engine_id.lower(), why)
            else:
                found[label] = (engine_id.lower(), argv, None)
            continue
        if _roster_line(argv):
            roster = True
            continue
        program = harness_program(argv)
        if program and "$" not in program:
            native = _native_engine(argv, program)
            if native is not None:
                why = _launch_unproven(argv, None)
                if why:
                    unproven[label] = (native, why)
                else:
                    found[label] = (native, argv, program)
                continue
        opaque[label] = "its launch program does not establish an engine"
    if found:
        unproven.update({label: ("", why) for label, why in opaque.items()})
    if unproven:
        engines = sorted({engine for engine, _ in unproven.values() if engine})
        detail = "; ".join(f"{label} {why}" for label, (_e, why) in unproven.items())
        return Engine("unproven", engines[0] if len(engines) == 1 else None,
                      f"{detail}, so its environment is not knowable here")
    ids = sorted({engine for engine, _, _ in found.values()})
    if roster and ids:
        return Engine("ambiguous", detail=f"lines mix a roster and engine {', '.join(ids)}")
    if roster:
        return Engine("roster")
    if not ids:
        return Engine("none")
    if len(ids) > 1:
        by_line = ", ".join(f"{label}: {found[label][0]}" for label in found)
        return Engine("ambiguous", detail=f"lines name different engines ({by_line})")
    lines = [(label, argv) for label, (_e, argv, _p) in found.items()]
    for label, (engine, _argv, program) in found.items():
        if program is None:
            continue
        if cwd is None:
            return Engine("unpinned", engine, f"{label} runs {program!r} and the node's working folder is unavailable")
        why = _same_binary(program, engine, env, cwd)
        if why:
            return Engine("unpinned", engine, f"{label} runs {program!r}, but {why}")
    return Engine("engine", ids[0], lines=lines)


MODEL_FLAGS = ("--model", "-m")
EFFORT_FLAGS = ("--effort", "--reasoning-effort", "--variant")
# codex carries its effort as a config override: `-c model_reasoning_effort=X`.
EFFORT_CONFIG_KEY = "model_reasoning_effort="


def _expand(value: str, variables: dict[str, str]) -> str:
    whole = PLACEHOLDER_RE.fullmatch(value)
    if whole is None:
        return value or "engine default"
    key = whole.group(1).upper()
    if key in variables and variables[key] != "":
        return variables[key]
    if whole.group(2):
        return "engine default"
    return f"unresolved: ${key}"


def _line_values(argv: list[str], flags: tuple[str, ...], config_key: str | None,
                 variables: dict[str, str]) -> set[str]:
    values: set[str] = set()
    for i, token in enumerate(argv):
        raw = None
        for flag in flags:
            if token.startswith(flag + "="):
                raw = token[len(flag) + 1:]
            elif token == flag and i + 1 < len(argv):
                raw = argv[i + 1]
        if (config_key and token == "-c" and i + 1 < len(argv)
                and argv[i + 1].startswith(config_key)):
            raw = argv[i + 1][len(config_key):]
        if raw is not None:
            values.add(_expand(raw, variables))
    return values or {"engine default"}


def _launch_setting(lines: list[tuple[str, list[str]]], flags: tuple[str, ...],
                    config_key: str | None, variables: dict[str, str]) -> str:
    """The setting the node's engine lines give, after argv vars expand, or
    `engine default` when none sets it. Lines (or repeats within a line) that
    disagree are reported as conflicting; no single value is invented."""
    per_line = {label: _line_values(argv, flags, config_key, variables) for label, argv in lines}
    distinct = set().union(*per_line.values()) if per_line else {"engine default"}
    if len(distinct) == 1:
        return next(iter(distinct))
    parts = "; ".join(f"{label}: {' / '.join(sorted(v))}" for label, v in per_line.items())
    return f"conflicting ({parts})"


# ---------- sections ----------

class _View:
    def __init__(self) -> None:
        self.findings: list[str] = []

    def find(self, text: str) -> None:
        self.findings.append(text)


def _show_definition(view: _View, name: str, info: dict) -> dict | None:
    raw = info.get("definition")
    path = Path(str(raw)).expanduser() if raw else default_definition_path("default")
    source = "registry" if raw else "default"
    print(f"definition: {definition_stem(str(path))} ({source}) {path}")
    try:
        return load_definition(name)
    except (OSError, RuntimeError, ValueError) as e:
        print(f"  unresolved: {_clean(e)}")
        view.find("definition unresolved")
        return None


def _show_variables(name: str, info: dict, definition: dict | None, variables: dict[str, str]) -> None:
    raw = info.get("vars") if isinstance(info.get("vars"), dict) else {}
    shown = [f"{k}={variables[k]}" for k in SHOWN_KNOBS if k in variables]
    print("vars: " + (" ".join(shown) if shown else "(none shown)"))
    hidden = sorted(k for k in variables if k not in SHOWN_KNOBS)
    if hidden:
        print(f"vars hidden: {', '.join(hidden)} (values: a8s vars {name})")
    env_names = sorted(
        n for k in raw if isinstance(k, str) and (n := split_env_var_key(k)) is not None
    )
    if env_names:
        print(f"env (values hidden): {', '.join(env_names)} (values: a8s vars {name})")
    if definition is not None:
        try:
            names = sorted(definition_env(definition))
        except ValueError:
            names = []
        if names:
            print(f"definition env (values hidden): {', '.join(names)}")


def _secret_values(raw: dict, definition: dict | None) -> tuple[str, ...]:
    values = [v for k, v in raw.items()
              if isinstance(k, str) and isinstance(v, str) and split_env_var_key(k) is not None]
    if definition is not None:
        try:
            values += list(definition_env(definition).values())
        except ValueError:
            pass
    return tuple(sorted({v for v in values if len(v) >= 6}, key=len, reverse=True))


def _show_runtime(view: _View, name: str, root_ok: bool) -> None:
    try:
        try:
            pid_path(name).parent.stat()
        except FileNotFoundError:
            pass
        pid = read_handler_pid_strict(name)
    except (OSError, ValueError) as e:
        print(f"runtime: unknown (cannot read pid file: {_clean(e)})")
        view.find("runtime unknown")
    else:
        if pid is None:
            print("runtime: stopped")
        else:
            up = _pid_uptime(name)
            print(f"runtime: running pid {pid}, up {'unknown' if up == '?' else up}")
    try:
        unresolved = unresolved_mailboxes().get(name)
    except Exception as e:
        unresolved = None
        print(f"routing: unknown ({_clean(e)})")
        view.find("routing unknown")
    else:
        if unresolved:
            print(f"routing: not routing ({_clean(unresolved)})")
            view.find("not routing")
        elif root_ok:
            print("routing: resolved")


def _show_activity(view: _View, name: str) -> None:
    """One status probe, however many facts read from it."""
    try:
        wake = read_wake_pid(name, strict=True)
    except (OSError, ValueError) as e:
        print(f"activity: unknown (wake record unreadable: {_clean(e)})")
        view.find("activity unknown")
    else:
        if wake is not None:
            pid, unit = wake
            what = "wake about to spawn" if pid == 0 else f"wake in flight, pid {pid}"
            print(f"activity: busy ({what}; {len(unit)} envelope(s))")
        else:
            configured, word = run_status_probe(name)
            if not configured:
                print("activity: idle (no status probe; no wake in flight)")
            elif word is None:
                print("activity: unknown (status probe failed, timed out or could not start)")
                view.find("activity unknown")
            elif word == "busy":
                print("activity: busy (resident turn in flight)")
            elif word == "resident":
                print("activity: resident idle (session up, no turn)")
            elif word in ("", "idle"):
                print("activity: idle")
            else:
                print("activity: unknown (status probe gave an unrecognised answer)")
                view.find("activity unknown")
    try:
        settled = last_wake_path(name).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        print("last wake: none recorded")
    except (OSError, UnicodeError):
        print("last wake: unknown (record unreadable)")
        view.find("last wake unknown")
    else:
        if settled == "failed":
            print("last wake: failed")
            view.find("last wake failed")
        elif settled == "ok":
            print("last wake: ok")
        else:
            print("last wake: unknown (invalid record)")
            view.find("last wake unknown")


def _tx(read, key: str):
    """`(value, None)` or `(None, reason)` for one transaction-log read."""
    try:
        return read().get(key), None
    except txlog.TransactionLogError as e:
        return None, _clean(e)


def _show_stats(view: _View, name: str) -> None:
    key = name.lower()
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    failed, why = _tx(lambda: txlog.count_events("WAKE_FAILED", since), key)
    if why:
        print(f"failed wakes (24 h): unknown ({why})")
        view.find("failed-wake count unknown")
    else:
        print(f"failed wakes (24 h): {failed or 0}")
        if failed:
            view.find(f"{failed} failed wake(s) in 24 h")
    # Counted here rather than through count_dead_letters, which also deletes
    # markers past their retention.
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp()
    try:
        dead = 0
        try:
            markers = list(dead_letters_dir(name).iterdir())
        except FileNotFoundError:
            markers = []
        for marker in markers:
            info = marker.stat()
            if stat.S_ISREG(info.st_mode) and info.st_mtime >= cutoff:
                dead += 1
    except OSError as e:
        print(f"dead letters (24 h): unknown ({_clean(e)})")
        view.find("dead-letter count unknown")
    else:
        print(f"dead letters (24 h): {dead}")
        if dead:
            view.find(f"{dead} dead letter(s) in 24 h")
    try:
        try:
            pending = sum(1 for p in inbox_dir(name).iterdir()
                          if p.name.endswith(".json") and stat.S_ISREG(p.stat().st_mode))
        except FileNotFoundError:
            pending = 0
        print(f"inbox pending: {pending}")
    except OSError as e:
        print(f"inbox pending: unknown ({_clean(e)})")
        view.find("inbox count unknown")
    landed, why = _tx(lambda: txlog.inbox_activity(since), key)
    if why:
        print(f"messages received (24 h): unknown ({why})")
        view.find("received count unknown")
    else:
        count, newest = landed if landed else (0, None)
        tail = f"; newest {clock.stamp(newest)}" if newest else ""
        print(f"messages received (24 h): {count}{tail}")
    sent, why = _tx(txlog.last_sent, key)
    if why:
        print(f"last sent: unknown ({why})")
        view.find("last-sent unknown")
    else:
        print(f"last sent: {clock.stamp(sent) if sent else 'never seen'} (observation, not health)")


# ---------- probes ----------

_SAFE_TEXT = re.compile(r"[A-Za-z0-9 ._:()/%+,-]+")
_TOKEN_RUN = re.compile(r"[A-Za-z0-9_=+/-]{24,}")
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,39}")


def _safe(value: object, limit: int) -> str | None:
    """The text itself when it is short, plain and shows no token-shaped run;
    otherwise None. Provider-supplied strings pass through this or not at all."""
    if not isinstance(value, str) or not 0 < len(value) <= limit:
        return None
    if _SAFE_TEXT.fullmatch(value) is None or _TOKEN_RUN.search(value):
        return None
    return value


def _real(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _probe_env(engine: str, env: dict[str, str]) -> dict[str, str]:
    """The environment for an r4t probe child. Muse's launcher updates the
    install in the background unless told not to; a read must not change it.
    The node's stored settings are not touched."""
    return {**env, "MUSE_NO_AUTO_UPDATE": "1"} if engine == "muse" else env


def _engine_ready(engine: str, env: dict[str, str], cwd: Path) -> tuple[bool | None, str]:
    """`(ready, text)` from `r4t engine <id> check --json`: True ready, False
    not runnable, None when the probe gave no usable answer. The check composes
    r4t's own default argv with no model, effort or flags from the node, so a
    pass proves the CLI is installed and takes that argv, nothing more. The
    report's free text is never printed: it is provider output."""
    repair = f"r4t engine {engine} check"
    argv = [sys.executable, str(R4T_PY), "engine", engine, "check", "--json"]
    _rc, text, problem = _run_bounded(argv, _probe_env(engine, env), cwd, CHECK_TIMEOUT_SECONDS)
    if problem:
        return None, f"{problem}; run `{repair}`"
    try:
        reports = json.loads(text)
    except ValueError:
        return None, f"check gave no readable report; run `{repair}`"
    report = reports[0] if isinstance(reports, list) and reports else None
    if (not isinstance(report, dict) or not isinstance(report.get("installed"), bool)
            or not isinstance(report.get("runnable"), bool)):
        return None, f"check gave a malformed report; run `{repair}`"
    if report["installed"] and report["runnable"]:
        return True, (f"{engine} installed and accepts r4t's default engine argv; "
                      "the node's own model, effort and flags are not tried")
    if not report["installed"]:
        return False, f"{engine} is not installed; run `{repair}`"
    return False, f"{engine} is installed but r4t's engine argv was not accepted; run `{repair}`"


def _live_quota(engine: str, env: dict[str, str], cwd: Path) -> dict:
    """Ask the sibling r4t for a live reading under the node's environment.
    `{"ok": True, "payload": ...}` for a live answer; otherwise `{"ok": False,
    "reason": str, "applicable": bool}`. A snapshot is never accepted."""
    argv = [sys.executable, str(R4T_PY), "engine", engine, "quota", "--live", "--json"]
    rc, text, problem = _run_bounded(
        argv, _probe_env(engine, env), cwd, QUOTA_TIMEOUT_SECONDS, QUOTA_TEARDOWN_GRACE_SECONDS
    )
    return _parse_quota(engine, rc, text, problem)


def _unavailable(reason: str, applicable: bool = True) -> dict:
    return {"ok": False, "reason": reason, "applicable": applicable}


def _bucket_problem(bucket: object) -> str | None:
    """Why a live bucket cannot be shown as measured, or None. The remaining
    fraction is a number in [0, 1] or None when the provider cannot express
    one. Measured `credits_used` remains useful without a denominator; a
    bucket with neither measure stays unknown and adds a finding."""
    if not isinstance(bucket, dict) or _safe(bucket.get("label"), 80) is None:
        return "a bucket has no usable label"
    reset = bucket.get("reset_time")
    if reset is not None and _safe(reset, 60) is None:
        return "a bucket has an unusable reset time"
    spent = bucket.get("credits_used")
    if spent is not None and not (_real(spent) and spent >= 0):
        return "a bucket has an invalid credits_used"
    if "remaining_fraction" not in bucket:
        return "a bucket has no remaining_fraction"
    fraction = bucket["remaining_fraction"]
    if fraction is None:
        return None
    if not (_real(fraction) and 0 <= fraction <= 1):
        return "a bucket has an invalid remaining_fraction"
    return None


def _parse_quota(engine: str, rc: int | None, text: str, problem: str | None) -> dict:
    """Free text from r4t or the provider is never relayed: failures print
    fixed wording, and at most a short lowercase `code` r4t names."""
    if problem:
        return _unavailable(problem)
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return _unavailable(f"r4t gave no readable quota answer (exit {rc})")
    if rc != 0:
        if data.get("origin") == "unavailable" and isinstance(data.get("reason"), str):
            if data.get("applicable") is False:
                return _unavailable("r4t reports this engine has no quota to read", applicable=False)
            hints = {
                "account_mismatch": "account mismatch: the quota adapter cannot use this node's account settings",
                "login_refresh_required": "the stored login needs refresh; run the engine once in the node's environment",
                "upgrade_required": "the installed engine is too old for live quota; upgrade it",
                "unsupported_binding": "the configured account cannot be bound to a live reading",
            }
            hint = hints.get(data.get("code"), "r4t could not take a live reading") if isinstance(data.get("code"), str) else "r4t could not take a live reading"
            return _unavailable(f"{hint}; run `r4t engine {engine} quota --live`")
        return _unavailable(f"r4t quota exited {rc} without a usable answer")
    if data.get("origin") != "live":
        return _unavailable("r4t returned a reading that is not live; refused")
    named = data.get("engine")
    if not isinstance(named, str) or named.lower() != engine:
        return _unavailable("the reading names a different engine than the node runs; refused")
    age = data.get("age_seconds")
    if age is not None and not (_real(age) and abs(age) <= SNAPSHOT_AGE_LIMIT_SECONDS):
        return _unavailable("the reading's age is invalid or too far from now; refused as stale")
    buckets = data.get("buckets")
    if not isinstance(buckets, list) or not buckets:
        return _unavailable("live reading carried no usable buckets")
    for bucket in buckets:
        why = _bucket_problem(bucket)
        if why:
            return _unavailable(f"live reading refused: {why}")
    return {"ok": True, "payload": data}


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _show_quota(view: _View, result: dict) -> None:
    if not result["ok"]:
        if not result.get("applicable", True):
            print(f"quota: not applicable ({result['reason']})")
            return
        print(f"quota: unavailable ({result['reason']})")
        view.find("quota unavailable")
        return
    payload = result["payload"]
    plan = _safe(payload.get("plan"), 80)
    print("quota: live" + (f" — {plan}" if plan else ""))
    for bucket in payload["buckets"]:
        label = bucket["label"]
        fraction = bucket["remaining_fraction"]
        reset_text = bucket.get("reset_time")
        if fraction is None:
            value = "remaining unknown"
            if bucket.get("credits_used") is None:
                view.find(f"{label} remaining unavailable")
        else:
            value = f"{round(fraction * 100)}% remaining"
            if fraction <= 0:
                until = f" until {reset_text}" if reset_text else ""
                view.find(f"{label} exhausted{until}")
                value += " (exhausted)"
        if bucket.get("credits_used") is not None:
            value += f" · {_number(bucket['credits_used'])} credits used"
        if reset_text:
            value += f" · resets {reset_text}"
        print(f"  {label}: {value}")


def _caveat(definition_env_names: set[str], node_env: dict[str, str]) -> list[str]:
    declared = set(node_env) | definition_env_names
    return [n for n in ACCOUNT_ENV_NAMES if n in os.environ and n not in declared]


# ---------- entry ----------

def _local(name: str, info: dict) -> int:
    view = _View()
    print(f"node: {name} (local)")
    definition = _show_definition(view, name, info)
    root_text = str(info.get("root") or "")
    cwd = Path(root_text).expanduser() if root_text else None
    root_ok = bool(cwd and cwd.is_dir())
    if root_ok:
        print(f"root: {root_text}")
    else:
        print(f"root: {root_text or '(none)'} (missing)")
        view.find("root missing")
        cwd = None

    raw_vars = info.get("vars") if isinstance(info.get("vars"), dict) else {}
    variables = argv_vars(raw_vars)
    secrets = _secret_values(raw_vars, definition)

    env: dict[str, str] | None = None
    env_problem: str | None = None
    node_env: dict[str, str] = {}
    def_names: set[str] = set()
    if definition is not None:
        try:
            if wake_shell(definition) is not None:
                env_problem = "the node wakes through a login shell, so its environment is not knowable here"
            else:
                node_env = load_agent_env(name)
                def_names = set(definition_env(definition))
                env = {**os.environ, **wake_env(definition, node_env)}
        except ValueError as e:
            env_problem = f"the node's environment cannot be composed: {_clean(e, secrets)}"

    if definition is None:
        engine = Engine("none")
        print("engine: unavailable (definition unresolved)")
    else:
        engine = _detect_engine(definition, env if env is not None else dict(os.environ), cwd)
        if engine.kind == "engine":
            print(f"engine: {engine.engine}")
            print(f"model: {_launch_setting(engine.lines, MODEL_FLAGS, None, variables)}")
            print(f"effort: {_launch_setting(engine.lines, EFFORT_FLAGS, EFFORT_CONFIG_KEY, variables)}")
        elif engine.kind == "roster":
            print("engine: none (roster node; engines belong to its rigs)")
        elif engine.kind == "none":
            print("engine: none")
        elif engine.kind == "ambiguous":
            print(f"engine: ambiguous ({engine.detail})")
            view.find("engine ambiguous")
        elif engine.kind == "unproven":
            print(f"engine: {engine.engine or 'unknown'} (launch context not provable: {engine.detail})")
            view.find("engine launch context unproven")
        else:
            print(f"engine: {engine.engine} (not pinned: {engine.detail})")
            view.find("engine not pinned")
    _show_variables(name, info, definition, variables)
    if env is not None and engine.kind == "engine":
        print("context: the launch environment a wake started from this shell would get "
              "(definition.env and env.* vars over this shell); not read from a live process")
        shell_only = _caveat(def_names, node_env)
        if shell_only:
            print(f"  note: inherited only from this shell: {', '.join(shell_only)}; a handler "
                  "started elsewhere may hold a different value")

    _show_runtime(view, name, root_ok)
    _show_activity(view, name)
    _show_stats(view, name)

    usable = env is not None and cwd is not None
    why_not = env_problem or ("the node's root folder is missing" if cwd is None else "")
    if engine.kind == "engine" and usable:
        ready, text = _engine_ready(engine.engine, env, cwd)
        if ready is True:
            print(f"readiness: ready ({text})")
        elif ready is False:
            print(f"readiness: FAIL ({text})")
            view.find("engine not runnable")
        else:
            print(f"readiness: unavailable ({text})")
            view.find("readiness unavailable")
    elif engine.kind == "engine":
        print(f"readiness: unavailable ({why_not or 'no environment'})")
        view.find("readiness unavailable")
    elif engine.kind in ("unproven", "unpinned", "ambiguous"):
        print(f"readiness: unavailable ({engine.detail})")
    else:
        print("readiness: not applicable (no engine to check)")

    print("transport: remotes are machine-wide, not specific to this node; storage checks are in bare `a8s health`")
    try:
        bad = _probe_remotes()
    except Exception as e:
        print(f"transport: unavailable ({_clean(e, secrets)})")
        view.find("transport unavailable")
    else:
        if bad:
            view.find(f"{bad} remote(s) failed")

    if engine.kind == "engine" and usable:
        if sys.stderr.isatty():
            print(f"quota: querying {engine.engine} live (up to {QUOTA_TIMEOUT_SECONDS:g}s)...", file=sys.stderr)
        _show_quota(view, _live_quota(engine.engine, env, cwd))
    elif engine.kind == "engine":
        print(f"quota: unavailable ({why_not or 'no environment'})")
        view.find("quota unavailable")
    elif engine.kind == "roster":
        print("quota: not applicable (roster node; quota belongs to each rig's engine)")
    elif engine.kind == "none" and definition is not None:
        print("quota: not applicable (no engine)")
    elif engine.kind in ("unpinned", "ambiguous", "unproven"):
        print(f"quota: unavailable ({engine.detail})")
        view.find("quota unavailable")
    else:
        print("quota: unavailable (definition unresolved)")
        view.find("quota unavailable")

    if view.findings:
        print(f"findings: {len(view.findings)} — {'; '.join(view.findings)}")
        return 1
    print("findings: none")
    return 0


def _remote(name: str, stamp: str) -> int:
    print(f"node: {name} (remote, remembered)")
    print(f"last heard: {clock.stamp(stamp)} (observation, not health)")
    for what in ("runtime", "engine", "quota"):
        print(f"{what}: unavailable (remote node; not known here)")
    print("findings: 1 — current remote runtime and quota are unavailable")
    return 1


def cmd_node_health(arg: str) -> int:
    match = resolve_recipient(arg)
    if match is not None:
        return _local(*match)
    try:
        kind, members = resolve_name(arg)
    except KeyError:
        pass
    except ValueError as e:
        _err(f"{_clean(e)}")
        return 1
    else:
        _err(f"{arg!r} is a{'n' if kind == 'alias' else ''} {kind}, not a node; name one of: {', '.join(members)}")
        return 1
    wanted = arg.strip().lower()
    for name, stamp in last_heard().items():
        if name.lower() == wanted:
            return _remote(name, stamp)
    _err(f"no node named {arg!r}")
    return 1
