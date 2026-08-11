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

RUN_ENGINES is narrower than `HARNESS_PRESETS`: only the five whose headless
invocation and continue-free single-shot behavior were verified live (see
the engine CLI fact sheet). opencode and the `*-ollama` launchers are not
here — nobody has checked what a launcher-wrapped or provider-delegating CLI
does on a bare unattended turn.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from rig import DEFAULT_TIMEOUT_SECONDS, HARNESS_PRESETS, RigError, build_preset_invoke, resolve_agy_model

__all__ = [
    "RUN_ENGINES",
    "DEFAULT_TIMEOUT_SECONDS",
    "IDLE_MARKER_NAME",
    "LESSONS_WARN_LINES",
    "DEFAULT_IDLE_PROMPT",
    "RunError",
    "build_argv",
    "scaffold_prompt",
    "execute",
]

RUN_ENGINES = frozenset({"claude", "codex", "agy", "copilot", "cursor"})

IDLE_MARKER_NAME = ".engine-idle"
LESSONS_WARN_LINES = 200
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


def warn_if_lessons_oversized(dir_path: Path) -> None:
    """Consolidation policy is pending (owner-ruled: this only warns). A
    missing or unreadable LESSONS.md is silently not-oversized."""
    try:
        text = (dir_path / "LESSONS.md").read_text(encoding="utf-8")
    except OSError:
        return
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    if lines > LESSONS_WARN_LINES:
        print(
            f"r4t engine: {dir_path / 'LESSONS.md'} is {lines} lines "
            f"(> {LESSONS_WARN_LINES}) — consolidation is manual for now",
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


def build_argv(engine: str, prompt: str, *, model: str | None, timeout: int) -> list[str]:
    """The final argv for one turn: the preset's own composition
    (rig.build_preset_invoke — the one source of argv truth) plus this
    module's unattended-turn additions, with `{prompt}` substituted last."""
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
    return [prompt if a == "{prompt}" else a for a in argv]


def _spawn(argv: list[str], cwd: Path, timeout: int) -> int:
    """Run `argv`, streaming its stdout/stderr through unchanged (no
    capture — the caller's fds are inherited). On timeout, kill the whole
    process group: a harness CLI commonly forks tool subprocesses that
    `proc.kill()` alone would leak."""
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            start_new_session=(os.name == "posix"),
        )
    except OSError as exc:
        raise RunError(f"failed to spawn {argv[0]!r}: {exc}") from exc
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()
        else:
            proc.kill()
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
) -> int:
    """Compose the turn's prompt and argv, run it, and return the CLI's own
    exit code (or 124 on a timeout kill)."""
    if scaffold:
        warn_if_lessons_oversized(dir_path)
        prompt = scaffold_prompt(dir_path, message, agent=agent)
    else:
        prompt = message
    argv = build_argv(engine, prompt, model=model, timeout=timeout)
    return _spawn(argv, dir_path, timeout)
