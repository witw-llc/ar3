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
import shlex
import signal
import subprocess
import sys
from pathlib import Path

from rig import DEFAULT_TIMEOUT_SECONDS, HARNESS_PRESETS, RigError, build_preset_invoke, resolve_agy_model

# The isolation test (apps/r4t/tests/docker/run-as.sh) copies apps/r4t alone
# into a container with no repo root, so `ark` is not always reachable there.
try:
    from ark.fsio import atomic_write_text as _atomic_write
except ImportError:
    def _atomic_write(path: Path, text: str) -> None:
        """Write `text` to `path` via a same-directory temp file + os.replace,
        so a killed turn never observes a half-written file."""
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

try:
    from ark.proc import spawn as _proc_spawn, terminate_group as _terminate_group
except ImportError:
    def _proc_spawn(argv: list[str], *, cwd: Path) -> subprocess.Popen:
        return subprocess.Popen(
            argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
            start_new_session=(os.name == "posix"),
        )

    def _terminate_group(proc: subprocess.Popen, *, grace_seconds: float = 0.5) -> None:
        # Mirrors ark.proc.terminate_group: the pgid is resolved once, before
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
    "claude", "codex", "agy", "copilot", "cursor", "opencode",
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


def _build_argv_template(
    engine: str, *, model: str | None, timeout: int, workdir: Path
) -> list[str]:
    """The final argv for one turn with `{prompt}` still unsubstituted: the
    preset's own composition (rig.build_preset_invoke — the one source of
    argv truth) plus this module's unattended-turn additions, with
    `{workdir}` substituted — opencode and ollama-opencode carry `--dir
    {workdir}`. `{prompt}` is left as a literal placeholder so a caller that
    only wants to display the argv (`--echo`) never has to guess which
    element was the prompt — value-matching a prompt equal to some other
    argv element (e.g. an engine literally named "claude") would otherwise
    elide the wrong one."""
    if engine not in RUN_ENGINES:
        raise RunError(
            f"r4t engine run supports {', '.join(sorted(RUN_ENGINES))}, "
            f"not {engine!r}"
        )
    try:
        argv = build_preset_invoke(engine, model=model)
    except RigError as exc:
        raise RunError(str(exc)) from exc
    if HARNESS_PRESETS[engine].get("model_resolver") == "agy-live" and "{model}" in argv:
        try:
            resolved = resolve_agy_model(model or "")
        except RigError as exc:
            raise RunError(f"agy --model {model!r} did not resolve: {exc}") from exc
        argv = [resolved if a == "{model}" else a for a in argv]
    extras = _run_extras(engine, argv, timeout)
    argv = argv[:1] + extras + argv[1:]
    return [str(workdir) if a == "{workdir}" else a for a in argv]


def build_argv(
    engine: str, prompt: str, *, model: str | None, timeout: int, workdir: Path
) -> list[str]:
    """The final, fully-substituted argv for one turn — `_build_argv_template`
    plus `{prompt}` substitution."""
    template = _build_argv_template(engine, model=model, timeout=timeout, workdir=workdir)
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


def _spawn(argv: list[str], cwd: Path, timeout: int) -> int:
    """Run `argv`, streaming its stdout/stderr through unchanged (no
    capture — the caller's fds are inherited). On timeout, terminate the
    whole process group (SIGTERM, a grace period, then SIGKILL): a harness
    CLI commonly forks tool subprocesses that `proc.kill()` alone would
    leak, and a grace period lets one that traps SIGTERM exit cleanly."""
    try:
        proc = _proc_spawn(argv, cwd=cwd)
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
) -> int:
    """Compose the turn's prompt and argv, run it, and return the CLI's own
    exit code (or 124 on a timeout kill). `echo` prints the composed argv and
    prompt to stderr before spawning — the turn still runs."""
    if scaffold:
        rotate_lessons_if_oversized(dir_path, lessons_cap)
        prompt = scaffold_prompt(dir_path, message, agent=agent)
    else:
        prompt = message
    template = _build_argv_template(engine, model=model, timeout=timeout, workdir=dir_path)
    if echo:
        _print_echo(template, prompt)
    argv = [prompt if a == "{prompt}" else a for a in template]
    return _spawn(argv, dir_path, timeout)
