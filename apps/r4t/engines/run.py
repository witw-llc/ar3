"""`r4t engine <id> run` — one headless turn of an engine CLI as a bare
stateless agent, invoked directly by an a8s node with no r4t roster or
dispatcher involved. r4t's own dispatcher (dispatch.run_harness) never calls
this: it already builds its own prompt, and stacking this module's scaffold
on top would double it.

Argv composition rides `rig.build_preset_invoke` — the one place that knows a
preset's `{prompt}`/`{model}` shape — with only the additions the fact sheet
says an unattended, roster-less turn needs on top: agy silently defaults
`--print-timeout` to 5 minutes (undercutting a longer `--timeout`), and an
unattended copilot hangs on its `ask_user` tool without `--no-ask-user`.

RUN_ENGINES is narrower than `HARNESS_PRESETS`: the five originals plus
opencode and three of the four `ollama-*` launchers have a verified headless,
continue-free single-shot invocation (see the engine CLI fact sheet). The
bare `ollama` preset stays excluded — `ollama run` has no file tools, and the
scaffold's read/write contract (`STATUS.md`, `LESSONS.md`) needs them.
`ollama-copilot` is excluded too: driven through `ollama launch copilot`,
every file write lands in copilot's session-state mirror
(`~/.copilot/session-state/<id>/files/`) instead of the real working
directory, which the scaffold's contract cannot survive; cloud `copilot`
stays in RUN_ENGINES since the quirk is specific to the launcher path.
"""
from __future__ import annotations

import os
import shutil
import shlex
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from rig import (
    DEFAULT_TIMEOUT_SECONDS,
    HARNESS_PRESETS,
    PERMISSION_MODES,
    RigError,
    apply_allowed_tools,
    apply_permissions,
    build_preset_invoke,
    continue_presets,
    continue_unsupported_reason,
    resolve_agy_model,
    splice_continue,
)

# The isolation test (apps/r4t/tests/docker/run-as.sh) copies apps/r4t alone
# into a container with no repo root, so `ar3` is not always reachable there.
try:
    from ar3.fsio import atomic_write_text as _atomic_write
except ImportError:
    def _atomic_write(path: Path, text: str) -> None:
        """Write `text` to `path` via a same-directory temp file + os.replace,
        so a killed turn never observes a half-written file."""
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

try:
    from ar3.proc import spawn as _proc_spawn, terminate_group as _terminate_group
except ImportError:
    def _proc_spawn(
        argv: list[str], *, cwd: Path, env: dict[str, str] | None = None
    ) -> subprocess.Popen:
        return subprocess.Popen(
            argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
            start_new_session=(os.name == "posix"), env=env,
        )

    def _terminate_group(proc: subprocess.Popen, *, grace_seconds: float = 0.5) -> None:
        # Mirrors ar3.proc.terminate_group: the pgid is resolved once, before
        # SIGTERM, so a leader that exits during the grace period cannot
        # strand SIGKILL with no pid left to resolve; pid stands in as the
        # pgid when getpgid cannot answer (true for any start_new_session
        # leader).
        if os.name != "posix":
            proc.kill()
            return
        pid = proc.pid
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = pid
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except OSError:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            if sig is signal.SIGTERM:
                import time as _time
                _time.sleep(grace_seconds)

__all__ = [
    "RUN_ENGINES",
    "PERMISSION_MODES",
    "DEFAULT_TIMEOUT_SECONDS",
    "IDLE_MARKER_NAME",
    "LESSONS_CAP_LINES",
    "LESSONS_ARCHIVE_NAME",
    "DEFAULT_IDLE_PROMPT",
    "RunError",
    "build_argv",
    "scaffold_prompt",
    "rotate_lessons_if_oversized",
    "execute",
]

RUN_ENGINES = frozenset({
    "claude", "codex", "agy", "copilot", "cursor", "opencode", "muse",
    "ollama-claude", "ollama-codex", "ollama-opencode",
})

IDLE_MARKER_NAME = ".engine-idle"
LESSONS_CAP_LINES = 200
LESSONS_ARCHIVE_NAME = "LESSONS-ARCHIVE.md"
TIMEOUT_EXIT_CODE = 124  # matches the `timeout(1)` convention

DEFAULT_IDLE_PROMPT = (
    "Idle pass: reconcile STATUS.md with reality, then append any new "
    "durable lessons to LESSONS.md. Keep both tight. If nothing needs "
    "doing, exit."
)


class RunError(Exception):
    """The turn could not be composed or started. The message says why."""


def scaffold_prompt(dir_path: Path, message: str, *, agent: str | None) -> str:
    """The fixed cold-boot prelude plus the volatile `message` last, so the
    prelude stays byte-identical across runs in the same `dir_path` and the
    prompt cache only ever misses on the routed input itself."""
    status = dir_path / "STATUS.md"
    agents_file = dir_path / "AGENTS.md"
    lessons = dir_path / "LESSONS.md"
    steps = [
        f"1. Read {status}, then {agents_file} and {lessons} if present. Use "
        "these absolute paths even if your workspace root differs. They are "
        "the durable source of truth; you have no transcript memory.",
    ]
    if agent:
        steps.append(
            f"{len(steps) + 1}. Run `a8s convo {agent}` and reconcile the "
            "newest routed messages with STATUS.md before acting."
        )
    steps.append(
        f"{len(steps) + 1}. Stay idle and exit unless there is clear "
        "direction or active work. Never restart completed work. Be "
        "token-frugal; no wordy prose."
    )
    steps.append(
        f"{len(steps) + 1}. Before exit, rewrite {status} with sections: "
        "Current State, Important Context, Next Steps, Decisions (with "
        f"rationale). Append genuinely new durable insights to {lessons} — "
        "append-only, one short bullet each, never rewrite or delete "
        "existing lessons. Never edit AGENTS.md."
    )
    prelude = "Smart cold boot:\n" + "\n".join(steps)
    return f"{prelude}\n\nRouted input:\n{message}"


def rotate_lessons_if_oversized(dir_path: Path, cap: int = LESSONS_CAP_LINES) -> None:
    """Option A — rotate, never merge; the file never meets a model. A
    LESSONS.md strictly over `cap` lines has its oldest lines moved out,
    whole lines only, so the live file lands at exactly `cap`. Moved lines
    are appended in order to LESSONS-ARCHIVE.md (created if absent) before
    LESSONS.md is rewritten, so a kill between the two writes can only
    duplicate lines into the archive, never lose them — each file's own
    write is atomic via temp file + os.replace, but the pair is not one
    transaction. A missing or unreadable LESSONS.md is silently not-
    oversized."""
    lessons_path = dir_path / "LESSONS.md"
    try:
        text = lessons_path.read_text(encoding="utf-8")
    except OSError:
        return
    trailing_newline = text.endswith("\n")
    lines = text.split("\n")
    if trailing_newline:
        lines.pop()
    if len(lines) <= cap:
        return
    overflow = len(lines) - cap
    moved, kept = lines[:overflow], lines[overflow:]

    archive_path = dir_path / LESSONS_ARCHIVE_NAME
    try:
        archive_prefix = archive_path.read_text(encoding="utf-8")
    except OSError:
        archive_prefix = ""
    if archive_prefix and not archive_prefix.endswith("\n"):
        archive_prefix += "\n"
    _atomic_write(archive_path, archive_prefix + "\n".join(moved) + "\n")
    _atomic_write(lessons_path, "\n".join(kept) + "\n")

    print(
        f"r4t engine: rotated {overflow} lines from {lessons_path} to {archive_path}",
        file=sys.stderr,
    )


def _run_extras(engine: str, base_invoke: list[str], timeout: int) -> list[str]:
    """Per-engine flags a bare unattended turn needs beyond what the preset
    already carries — checked against the live preset, not assumed, so a
    preset gaining the flag later does not double it."""
    extras: list[str] = []
    if engine == "agy":
        extras += ["--print-timeout", f"{timeout}s"]
    if engine == "copilot" and "--no-ask-user" not in base_invoke:
        extras += ["--no-ask-user"]
    return extras


def _splice_continue(engine: str, argv: list[str]) -> list[str]:
    """The preset's own continuation tokens at its own anchor — the roster
    path's rule (`Rig.argv`), applied to a roster-less turn. An engine with no
    verified continuation, or an anchor its invoke no longer carries, fails
    closed and names the engines that can, because a turn that silently starts
    cold when the caller asked to continue is the failure that costs a whole
    context reload."""
    preset = HARNESS_PRESETS[engine]
    tokens = list(preset.get("continue_argv", ()))
    anchor = preset.get("continue_anchor")
    if not tokens or (anchor is not None and anchor not in argv):
        raise RunError(
            f"{engine} cannot continue: {continue_unsupported_reason(engine)} "
            f"(engines that can: "
            f"{', '.join(e for e in continue_presets() if e in RUN_ENGINES)})"
        )
    return splice_continue(
        argv,
        tokens=tokens,
        anchor=anchor,
        drop_pair=preset.get("continue_drop_pair", ()),
    )


def _build_argv_template(
    engine: str,
    *,
    model: str | None,
    timeout: int,
    workdir: Path,
    continue_conversation: bool = False,
    permissions: str | None = None,
    allowed_tools: str | None = None,
) -> tuple[list[str], str | None]:
    """The final argv for one turn with `{prompt}` still unsubstituted, plus
    the one stderr note a requested permissions mode earns: the preset's own
    composition (rig.build_preset_invoke — the one source of argv truth), the
    permission/allowlist translation the caller asked for, this module's
    unattended-turn additions, and `{workdir}` substituted — opencode and
    ollama-opencode carry `--dir {workdir}`. `{prompt}` is left as a literal
    placeholder so a caller that only wants to display the argv (`--echo`)
    never has to guess which element was the prompt — value-matching a prompt
    equal to some other argv element (e.g. an engine literally named "claude")
    would otherwise elide the wrong one."""
    if engine not in RUN_ENGINES:
        raise RunError(
            f"r4t engine run supports {', '.join(sorted(RUN_ENGINES))}, "
            f"not {engine!r}"
        )
    try:
        argv = build_preset_invoke(engine, model=model)
        argv, note = apply_permissions(argv, engine, permissions, where="r4t engine: ")
        argv = apply_allowed_tools(argv, engine, allowed_tools, where="r4t engine: ")
    except RigError as exc:
        raise RunError(str(exc)) from exc
    if HARNESS_PRESETS[engine].get("model_resolver") == "agy-live" and "{model}" in argv:
        try:
            resolved = resolve_agy_model(model or "")
        except RigError as exc:
            raise RunError(f"agy --model {model!r} did not resolve: {exc}") from exc
        argv = [resolved if a == "{model}" else a for a in argv]
    if continue_conversation:
        argv = _splice_continue(engine, argv)
    extras = _run_extras(engine, argv, timeout)
    argv = argv[:1] + extras + argv[1:]
    return [str(workdir) if a == "{workdir}" else a for a in argv], note


def build_argv(
    engine: str,
    prompt: str,
    *,
    model: str | None,
    timeout: int,
    workdir: Path,
    continue_conversation: bool = False,
    permissions: str | None = None,
    allowed_tools: str | None = None,
) -> list[str]:
    """The final, fully-substituted argv for one turn — `_build_argv_template`
    plus `{prompt}` substitution."""
    template, _ = _build_argv_template(
        engine,
        model=model,
        timeout=timeout,
        workdir=workdir,
        continue_conversation=continue_conversation,
        permissions=permissions,
        allowed_tools=allowed_tools,
    )
    return [prompt if a == "{prompt}" else a for a in template]


def _print_echo(template: list[str], prompt: str) -> None:
    """`--echo`: the exact argv and prompt a turn is about to run, on stderr
    so stdout stays the engine's own reply stream. The turn still runs —
    this is an echo, not a dry-run. `template` still carries `{prompt}` as a
    literal placeholder (never value-matched against argv elements, so an
    engine literally named the same as the prompt is not elided); the
    prompt block below it is the one full copy."""
    print(f"r4t engine echo: argv: {shlex.join(template)}", file=sys.stderr)
    print("r4t engine echo: --- prompt ---", file=sys.stderr)
    print(prompt, file=sys.stderr)
    print("r4t engine echo: --- end prompt ---", file=sys.stderr)


def resolve_argv0(argv: list[str]) -> list[str]:
    """`argv` with a bare program name resolved to the path it runs from.

    Windows' CreateProcess appends only `.exe` to a bare name, never `.cmd` —
    and every npm global install arrives as a `.cmd` shim, which is how codex,
    opencode and cursor are installed. So `shutil.which` finds the CLI, the
    check reports it installed, and the exec then fails with WinError 2. Both
    halves are true at once, which is why it reads as "installed but
    unverifiable" rather than as a missing binary.

    Resolved here, at the moment the argv is handed to the OS, rather than at
    composition — the argv r4t echoes and reports stays the readable name the
    operator would type. A path (already resolved, or given as one) is left
    alone, and a name that resolves to nothing is left for the OS to reject
    with its own error.
    """
    if not argv:
        return argv
    program = argv[0]
    if os.sep in program or (os.altsep and os.altsep in program):
        return argv
    resolved = shutil.which(program)
    return [resolved, *argv[1:]] if resolved else argv


def _spawn(
    argv: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None
) -> int:
    """Run `argv`, streaming its stdout/stderr through unchanged (no
    capture — the caller's fds are inherited). On timeout, terminate the
    whole process group (SIGTERM, a grace period, then SIGKILL): a harness
    CLI commonly forks tool subprocesses that `proc.kill()` alone would
    leak, and a grace period lets one that traps SIGTERM exit cleanly."""
    try:
        proc = _proc_spawn(resolve_argv0(argv), cwd=cwd, env=env)
    except FileNotFoundError as exc:
        path = (env or os.environ).get("PATH", "")
        raise RunError(
            f"failed to spawn {argv[0]!r}: not on PATH ({path})"
        ) from exc
    except OSError as exc:
        raise RunError(f"failed to spawn {argv[0]!r}: {exc}") from exc
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_group(proc)
        proc.wait()
        print(f"r4t engine: turn timed out after {timeout}s", file=sys.stderr)
        return TIMEOUT_EXIT_CODE
    return proc.returncode


def execute(
    engine: str,
    message: str,
    *,
    dir_path: Path,
    model: str | None,
    agent: str | None,
    timeout: int,
    scaffold: bool,
    echo: bool = False,
    lessons_cap: int = LESSONS_CAP_LINES,
    continue_conversation: bool = False,
    permissions: str | None = None,
    allowed_tools: str | None = None,
    env: dict[str, str] | None = None,
    charge_hook: Callable[[], None] | None = None,
) -> int:
    """Compose the turn's prompt and argv, run it, and return the CLI's own
    exit code (or 124 on a timeout kill). `echo` prints the composed argv and
    prompt to stderr before spawning — the turn still runs. A requested
    `permissions` mode the engine answers above the asked-for tier prints one
    note; a mode below its floor never gets here (RunError). `env` is the
    child's whole environment when given (None inherits this process's) —
    `r4t rig run` passes the rig's `env` map layered over `os.environ`.
    `charge_hook` runs after composition succeeds and immediately before the
    spawn: a turn refused at composition costs the caller nothing, while a
    harness that fails to start has already paid — the same boundary a
    dispatched turn's budget draws."""
    if scaffold:
        rotate_lessons_if_oversized(dir_path, lessons_cap)
        prompt = scaffold_prompt(dir_path, message, agent=agent)
    else:
        prompt = message
    template, note = _build_argv_template(
        engine,
        model=model,
        timeout=timeout,
        workdir=dir_path,
        continue_conversation=continue_conversation,
        permissions=permissions,
        allowed_tools=allowed_tools,
    )
    if note:
        print(f"r4t engine: {note}", file=sys.stderr)
    if echo:
        _print_echo(template, prompt)
    argv = [prompt if a == "{prompt}" else a for a in template]
    if charge_hook is not None:
        charge_hook()
    return _spawn(argv, dir_path, timeout, env)
