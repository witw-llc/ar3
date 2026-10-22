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

A copilot turn also carries per-turn INSTRUMENTS — a `--usage-output-file`
path, an exporter path, and the rig's spend fuse. They are not preset
constants: the paths change every turn, and the usage flag is 1.0.82+, so a
preset that always carried it would make `engine check` reject the preset on
an older seat. Arming and reading them is `engines.copilot.turn_instruments`,
which the roster's own turn (`dispatch.run_harness`) calls too — one
implementation, two callers, so the two paths cannot drift apart.

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

import hashlib
import os
import shutil
import shlex
import signal
import subprocess
import sys
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from engines import copilot as copilot_engine
from rig import (
    DEFAULT_TIMEOUT_SECONDS,
    HARNESS_PRESETS,
    PERMISSION_MODES,
    Rig,
    RigError,
    apply_allowed_tools,
    apply_mcp,
    apply_permissions,
    build_preset_invoke,
    continue_presets,
    continue_unsupported_reason,
    instrumented_presets,
    resolve_agy_model,
    session_presets,
    session_tokens,
    splice_continue,
    splice_session,
)

# The isolation test (apps/r4t/tests/docker/run-as.sh) copies apps/r4t alone
# into a container with no repo root, so `ar3` is not always reachable there.
try:
    from ar3.fsio import atomic_write_bytes as _atomic_write_bytes
except ImportError:
    def _atomic_write_bytes(path: Path, data: bytes) -> None:
        """Write `data` to `path` via a same-directory temp file + os.replace,
        so a killed turn never observes a half-written file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

try:
    from ar3.proc import missing_interpreter
except ImportError:
    def missing_interpreter(program: Path | str, path: str | None = None) -> str | None:
        return None

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
    "LESSONS_CAP_BYTES",
    "LESSONS_WARN_AT",
    "LESSONS_TARGET",
    "LESSONS_ARCHIVE_NAME",
    "MCP_TELL_STEP",
    "CODEX_MCP_TELL_STEP",
    "CLI_TELL_STEP",
    "mcp_tell_step",
    "DEFAULT_IDLE_PROMPT",
    "RunError",
    "build_argv",
    "git_identity_env",
    "scaffold_prompt",
    "prepare_lessons_copies",
    "execute",
]

RUN_ENGINES = frozenset({
    "claude", "codex", "agy", "copilot", "cursor", "opencode", "muse", "devin",
    "ollama-claude", "ollama-codex", "ollama-opencode",
})

IDLE_MARKER_NAME = ".engine-idle"
LESSONS_CAP_LINES = 200
# Lines run past 1,000 characters in the field, so a line cap alone does not
# bound what a turn reads.
LESSONS_CAP_BYTES = 35 * 1024
# Past this fraction of either cap the startup has the agent consolidate, to
# at most the target fraction, before it answers anything.
LESSONS_WARN_AT = 0.9
LESSONS_TARGET = 0.75
LESSONS_ARCHIVE_NAME = "LESSONS-ARCHIVE.md"
LESSONS_COPY_DIR = "archive"
TIMEOUT_EXIT_CODE = 124  # matches the `timeout(1)` convention

# The idioms that splice the a8s MCP server into argv alone. The file idioms
# write a config beside the outbox, and an a8s node's outbox sits inside the
# agent's own directory, so those engines keep the shell `tell`.
RUN_MCP_IDIOMS = frozenset({"claude-flag", "codex-config", "copilot-flag"})

# Both steps name the tool verbatim: a tool described generically goes unused
# on small models, while the named one was called 20/20.
MCP_TELL_STEP = (
    "Reply only by message: call the `a8s_tell` MCP tool with `recipient` "
    "(the name) and `body` (your message), and `attachments` (a list of "
    "absolute paths) to send files with it. Printing text sends nothing, and "
    "no one reads this session's output. The body is delivered byte-exact; no "
    "shell is involved. If `a8s_tell` is unavailable, fall back to the `tell` "
    "CLI with body on stdin (`tell <recipient> - <<'EOF'`). Answer the sender "
    "named in the routed input. Delivery is asynchronous; do not wait for a "
    "reply."
)

# Codex lists MCP tools as `mcp__<server>__<tool>` inside its code-mode tool
# catalog, so a model told `a8s_tell` searches for that name, finds nothing,
# and falls back to the shell — which a sandbox that cannot start leaves
# without a way to reply (codex-cli 0.163.0, live, 2026-10-22).
CODEX_MCP_TELL_TOOL = "mcp__a8s__tell"
CODEX_MCP_TELL_STEP = MCP_TELL_STEP.replace(
    "call the `a8s_tell` MCP tool",
    f"call the a8s MCP server's `tell` tool (`{CODEX_MCP_TELL_TOOL}` in "
    "your tool list)",
).replace("If `a8s_tell` is unavailable", "If that tool is unavailable")
assert CODEX_MCP_TELL_STEP != MCP_TELL_STEP


def mcp_tell_step(engine: str) -> str:
    """The MCP tell step in the tool name the engine shows its model."""
    if HARNESS_PRESETS.get(engine, {}).get("mcp") == "codex-config":
        return CODEX_MCP_TELL_STEP
    return MCP_TELL_STEP


CLI_TELL_STEP = (
    "Reply only by message: run the `tell` shell command via your shell tool. "
    "Printing text sends nothing, and no one reads this session's output. Pass "
    "the body on stdin with a quoted heredoc so `$`, `` ` ``, and `\\` arrive "
    "byte-exact (`tell <recipient> - <<'EOF'`). If the `a8s` MCP server is "
    "configured in this session, you may call its `tell` tool instead "
    "(`recipient`, `body`, optional `attachments`). Answer the sender named in "
    "the routed input. Delivery is asynchronous; do not wait for a reply."
)

LESSON_LINE_RULE = (
    "Every line of it is one lesson, one short line each, with no sections, "
    "headings or tiers. Append; edit an existing line only when told to "
    "consolidate."
)

DEFAULT_IDLE_PROMPT = (
    "Idle pass: reconcile STATUS.md with reality, then append any new "
    "durable lessons to LESSONS.md. Keep both tight. If nothing needs "
    "doing, exit."
)


class RunError(Exception):
    """The turn could not be composed or started. The message says why."""


_GIT_IDENTITY_FLAGS = (
    ("--git-name", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
    ("--git-email", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")),
)
_GIT_IDENT_REFUSED = ("\n", "\r", "\0", "<", ">")


def git_identity_env(name: str | None, email: str | None) -> dict[str, str]:
    """The git environment names one turn commits under. `name` sets the
    author and committer name, `email` both emails, each independently. git's
    environment beats every config file in any repo, so a new commit the turn
    makes names these as author and committer. `git commit --amend` and
    `git rebase` keep a rewritten commit's original author and name these as
    its committer only. An unset or blank value adds nothing and leaves git's
    own config in charge. A value with a line break or NUL, or with `<` or
    `>`, is refused: git drops the ident delimiters silently, so the commit
    would name something the operator never typed."""
    identity: dict[str, str] = {}
    for (flag, names), value in zip(_GIT_IDENTITY_FLAGS, (name, email)):
        if value is None or not value.strip():
            continue
        if any(c in value for c in _GIT_IDENT_REFUSED):
            raise RunError(
                f"{flag} must be one line of text without '<' or '>' (git "
                f"drops those from an identity): {value!r}"
            )
        identity.update(dict.fromkeys(names, value))
    return identity


def scaffold_prompt(
    dir_path: Path,
    message: str,
    *,
    agent: str | None,
    tell_step: str = CLI_TELL_STEP,
    resident_minutes: int | None = None,
    lessons_cap: int = LESSONS_CAP_LINES,
    lessons_cap_bytes: int = LESSONS_CAP_BYTES,
    copies: list[Path] | None = None,
) -> str:
    """The fixed cold-boot prelude, then this turn's LESSONS.md note, then the
    volatile `message` last. One prelude starts a one-shot turn and a resident
    session (`resident_minutes` set) alike; only the closing steps differ. It
    stays byte-identical across runs in the same `dir_path`, so the prompt
    cache only ever misses on what follows it. The note changes turn to turn,
    so it sits after the prelude: the consolidation `_lessons_note` composes
    from `copies`, when one is due. Only a named `agent` is on the network, so
    only it gets `tell_step`; a turn run at a terminal answers on stdout."""
    status = dir_path / "STATUS.md"
    agents_file = dir_path / "AGENTS.md"
    lessons = dir_path / "LESSONS.md"
    steps = [
        f"Read {status}, then {agents_file} and {lessons} if present. Use "
        "these absolute paths even if your workspace root differs. They are "
        "the durable source of truth; you have no transcript memory."
    ]
    if agent:
        steps.append(
            f"You are '{agent}'. Run `a8s convo {agent}` and reconcile the "
            "newest routed messages with STATUS.md before acting."
        )
        steps.append(tell_step)
    sections = (
        "Current State, Important Context, Next Steps, Decisions (with rationale)"
    )
    if resident_minutes is None:
        title = "Smart cold boot:"
        steps.append(
            "Stay idle and exit unless there is clear direction or active "
            "work. Never restart completed work. Be token-frugal; no wordy prose."
        )
        steps.append(
            f"Before exit, rewrite {status} with sections: {sections}. Append "
            f"genuinely new durable insights to {lessons}. {LESSON_LINE_RULE} "
            "Never edit AGENTS.md."
        )
    else:
        title = "Smart cold boot (resident session):"
        steps.append(
            f"Resident session: it stays open while a turn is running and "
            f"closes after about {resident_minutes}m of quiet (no turn in "
            "flight, no new message). New inbound "
            "messages are pushed directly into this conversation as subsequent "
            "user turns — do NOT start a background `tells` monitor or poll "
            "loop. Handle each message or burst as it arrives and finish your "
            "turn so the next message can be delivered. Be token-frugal; no "
            "wordy prose."
        )
        steps.append(
            "When the session window expires, you will receive a wrap-up turn "
            f"to rewrite {status} (sections: {sections}) and append genuinely "
            f"new durable insights to {lessons}. {LESSON_LINE_RULE} Never edit "
            "AGENTS.md."
        )
    prelude = title + "\n" + "\n".join(
        f"{n}. {step}" for n, step in enumerate(steps, 1)
    )
    note = _lessons_note(dir_path, lessons_cap, lessons_cap_bytes, copies)
    tail = f"\n\n{note}" if note else ""
    return f"{prelude}{tail}\n\nRouted input:\n{message}"


def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _lessons_text(data: bytes) -> str | None:
    """A lessons file as text with LF line endings, or None when it is not
    UTF-8. Every size r4t reports counts LF endings."""
    try:
        return _lf(data).decode("utf-8")
    except UnicodeDecodeError:
        return None


def _read_lessons(path: Path) -> str | None:
    try:
        return _lessons_text(path.read_bytes())
    except OSError:
        return None


def _lessons_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    return lines


def _kb(size: int) -> str:
    text = f"{size / 1024:.1f}"
    return text[:-2] if text.endswith(".0") else text


def _past_warning(lines: int, size: int, cap: int, cap_bytes: int) -> bool:
    return lines > cap * LESSONS_WARN_AT or size > cap_bytes * LESSONS_WARN_AT


def _spillover_lines(dir_path: Path) -> int:
    """The non-blank lines of the spillover file, which no turn reads."""
    text = _read_lessons(dir_path / LESSONS_ARCHIVE_NAME)
    return sum(1 for line in _lessons_lines(text) if line.strip()) if text else 0


def _consolidation_due(dir_path: Path, cap: int, cap_bytes: int) -> bool:
    text = _read_lessons(dir_path / "LESSONS.md")
    if text is not None and _past_warning(
        len(_lessons_lines(text)), len(text.encode("utf-8")), cap, cap_bytes
    ):
        return True
    return _spillover_lines(dir_path) > 0


def _copy_day() -> str:
    """The copy's date, UTC like every date in a filename: it names files
    that sort, so it cannot move with the machine's zone."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def prepare_lessons_copies(
    dir_path: Path,
    *,
    lessons_cap: int = LESSONS_CAP_LINES,
    lessons_cap_bytes: int = LESSONS_CAP_BYTES,
) -> list[Path] | None:
    """Ready this turn's consolidation. When one is due (LESSONS.md past the
    warning line of either cap, or a spillover file with lines in it), save
    each of the two files that exists to archive/<name>-<date>-<sha>.md and
    return the copies. The copy keeps the consolidation lossless: a line the
    model merges or drops is still on disk. So the rewrite is granted only
    once every copy reads back equal to its file. The name carries the
    content's hash, so a turn that finds the same bytes reuses the copy
    instead of writing another. Only a file that does not exist is skipped.
    None when nothing is due, a file that exists cannot be read, or a copy
    could not be verified; the turn then gets no permission to edit."""
    if not _consolidation_due(dir_path, lessons_cap, lessons_cap_bytes):
        return None
    copies: list[Path] = []
    folder = dir_path / LESSONS_COPY_DIR
    for name in ("LESSONS.md", LESSONS_ARCHIVE_NAME):
        path = dir_path / name
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue
        except OSError as exc:
            print(f"r4t engine: no consolidation this turn: {exc}", file=sys.stderr)
            return None
        digest = hashlib.sha256(data).hexdigest()[:8]
        copy = folder / f"{Path(name).stem}-{_copy_day()}-{digest}.md"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if not (copy.exists() and copy.read_bytes() == data):
                _atomic_write_bytes(copy, data)
            verified = copy.read_bytes() == data == path.read_bytes()
        except OSError as exc:
            print(f"r4t engine: no consolidation this turn: {exc}", file=sys.stderr)
            return None
        if not verified:
            print(
                f"r4t engine: no consolidation this turn: {copy} does not "
                f"match {path}",
                file=sys.stderr,
            )
            return None
        copies.append(copy)
    return copies


def _lessons_note(
    dir_path: Path, cap: int, cap_bytes: int, copies: list[Path] | None
) -> str:
    """This turn's LESSONS.md note: the consolidation when one is due and
    `copies` holds its verified copies, a warning without permission to edit
    when one is due and no copy exists, else nothing."""
    if not _consolidation_due(dir_path, cap, cap_bytes):
        return ""
    lessons = dir_path / "LESSONS.md"
    archive = dir_path / LESSONS_ARCHIVE_NAME
    text = _read_lessons(lessons) or ""
    lines = len(_lessons_lines(text)) if text else 0
    size = len(text.encode("utf-8"))
    figures = (
        f"{lessons} is {lines} lines / {_kb(size)} KB; the cap is {cap} lines / "
        f"{_kb(cap_bytes)} KB"
    )
    spilled = _spillover_lines(dir_path)
    if copies is None:
        return (
            f"{figures}. r4t could not save a copy of it, so do not edit "
            "existing lines this turn, and do not append a lesson that repeats "
            "one already here."
        )
    target_lines = int(cap * LESSONS_TARGET)
    target_kb = _kb(int(cap_bytes * LESSONS_TARGET))
    rules = [
        "- Every line is one lesson. Remove sections, headings and tiers.",
        "- Merge each duplicate into the lesson it repeats.",
        "- Drop a lesson only when a newer lesson supersedes it. Never drop a "
        "live rule.",
    ]
    if spilled:
        rules.append(
            f"- {archive} holds {spilled} lines that no turn reads. Fold the "
            f"lessons that still hold into {lessons}, then delete {archive}."
        )
    return "\n".join([
        f"Consolidate {lessons} before you answer any message. {figures}. "
        f"Rewrite it to at most {target_lines} lines and {target_kb} KB.",
        *rules,
        "Copies from before this turn: "
        + ", ".join(str(copy) for copy in copies)
        + ". This is the one rewrite of LESSONS.md that the append-only rule "
        "allows.",
    ])


def _run_extras(engine: str, base_invoke: list[str], timeout: int) -> list[str]:
    """Per-engine flags a bare unattended turn needs beyond what the preset
    already carries — checked against the live preset, not assumed, so a
    preset gaining the flag later does not double it. A turn's instruments are
    NOT here: they are per-turn values both callers arm identically, and they
    arrive already composed as `instruments.flags()`."""
    extras: list[str] = []
    if engine == "agy":
        extras += ["--print-timeout", f"{timeout}s"]
    if engine == "copilot" and "--no-ask-user" not in base_invoke:
        extras += ["--no-ask-user"]
    return extras


def _reject_bad_max_credits(engine: str, max_credits: int) -> None:
    """A spend fuse only an instrumented preset expresses, and only above its
    engine's floor. Both refusals are composition errors rather than notes: a
    cap the caller believes is in force and is not is worse than no cap."""
    if not HARNESS_PRESETS[engine].get("instruments"):
        raise RunError(
            f"{engine} takes no --max-credits: it expresses no spend fuse "
            f"(presets that do: {', '.join(instrumented_presets())})"
        )
    problem = copilot_engine.max_credits_problem(max_credits)
    if problem:
        raise RunError(problem)


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
            f"{', '.join(e for e in continue_presets() if e in RUN_ENGINES and e != engine)})"
        )
    return splice_continue(
        argv,
        tokens=tokens,
        anchor=anchor,
        drop_pair=preset.get("continue_drop_pair", ()),
    )


def _splice_session(engine: str, argv: list[str], session: str) -> list[str]:
    """`argv` pinned to `session`. The founding turn names the id with
    `--session-id`, every later one drives `--resume=<id>` with the workdir
    forced — a resumed copilot session otherwise runs in the directory it was
    founded in, whatever the invoking cwd. Which of the two applies is read off
    the CLI's own session store rather than tracked here, so a run can be
    resumed by any caller that knows the id.

    copilot is the only preset with a pin, and the store it is looked up in is
    copilot's. A second pinned engine would want that lookup on the preset;
    with one, naming it here is the honest spelling.
    """
    if not session_tokens(engine, resume=True):
        raise RunError(
            f"{engine} takes no --session pin (engines that do: "
            f"{', '.join(e for e in session_presets() if e in RUN_ENGINES)})"
        )
    return splice_session(
        argv,
        tokens=session_tokens(
            engine, resume=copilot_engine.session_exists(session)
        ),
        session=session,
    )


def _build_argv_template(
    engine: str,
    *,
    model: str | None,
    effort: str | None = None,
    timeout: int,
    workdir: Path,
    continue_conversation: bool = False,
    permissions: str | None = None,
    allowed_tools: str | None = None,
    session: str | None = None,
    instruments: copilot_engine.TurnInstruments | None = None,
    max_credits: int | None = None,
    resident: bool = False,
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
    if max_credits is not None:
        _reject_bad_max_credits(engine, max_credits)
    if session is not None and continue_conversation:
        raise RunError(
            f"--session and --continue contradict for {engine}: the session id "
            "IS the continuation, and it says which conversation"
        )
    if resident:
        from engines.resident import RESIDENT_ENGINES

        if engine not in RESIDENT_ENGINES:
            raise RunError(
                f"{engine} does not support --resident (engines that do: "
                f"{', '.join(sorted(RESIDENT_ENGINES))})"
            )
        if continue_conversation or session is not None:
            raise RunError(
                "--resident cannot be combined with --continue or --session: "
                "a resident session holds its own live conversation"
            )
    try:
        argv = build_preset_invoke(engine, model=model, effort=effort)
        argv, note = apply_permissions(argv, engine, permissions, where="r4t engine: ")
        argv = apply_allowed_tools(argv, engine, allowed_tools, where="r4t engine: ")
    except RigError as exc:
        raise RunError(str(exc)) from exc
    if HARNESS_PRESETS[engine].get("model_resolver") == "agy-live" and "{model}" in argv:
        try:
            resolved = resolve_agy_model(model or "", effort=effort)
        except RigError as exc:
            raise RunError(f"agy --model {model!r} did not resolve: {exc}") from exc
        argv = [resolved if a == "{model}" else a for a in argv]
    if continue_conversation:
        argv = _splice_continue(engine, argv)
    if session is not None:
        argv = _splice_session(engine, argv, session)
    extras = _run_extras(engine, argv, timeout)
    if instruments is not None:
        extras += instruments.flags()
    argv = argv[:1] + extras + argv[1:]
    return [str(workdir) if a == "{workdir}" else a for a in argv], note


def build_argv(
    engine: str,
    prompt: str,
    *,
    model: str | None,
    effort: str | None = None,
    timeout: int,
    workdir: Path,
    continue_conversation: bool = False,
    permissions: str | None = None,
    allowed_tools: str | None = None,
    session: str | None = None,
) -> list[str]:
    """The final, fully-substituted argv for one turn — `_build_argv_template`
    plus `{prompt}` substitution."""
    template, _ = _build_argv_template(
        engine,
        model=model,
        effort=effort,
        timeout=timeout,
        workdir=workdir,
        continue_conversation=continue_conversation,
        permissions=permissions,
        allowed_tools=allowed_tools,
        session=session,
    )
    return [prompt if a == "{prompt}" else a for a in template]


def _print_echo(
    template: list[str], prompt: str, env_added: dict[str, str] | None = None
) -> None:
    """`--echo`: the exact argv and prompt a turn is about to run, on stderr
    so stdout stays the engine's own reply stream. The turn still runs —
    this is an echo, not a dry-run. `template` still carries `{prompt}` as a
    literal placeholder (never value-matched against argv elements, so an
    engine literally named the same as the prompt is not elided); the
    prompt block below it is the one full copy. `env_added` is what the turn
    sets on the child's environment beyond what it inherits (the git
    identity), one line when there is any."""
    print(f"r4t engine echo: argv: {shlex.join(template)}", file=sys.stderr)
    if env_added:
        pairs = " ".join(f"{k}={shlex.quote(v)}" for k, v in env_added.items())
        print(f"r4t engine echo: env: {pairs}", file=sys.stderr)
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


def interpreter_gap(program: str, path: str | None = None) -> str | None:
    """One sentence naming the interpreter `program` cannot start without,
    when `program` resolves on `path` to a script and its interpreter does
    not. None when the launch is whole, or when `program` does not resolve —
    a missing binary has its own message."""
    resolved = program if os.sep in program else shutil.which(program, path=path)
    if not resolved:
        return None
    missing = missing_interpreter(resolved, path)
    if missing is None:
        return None
    return (
        f"{program} is {resolved}, a script that runs under {missing}, "
        f"and {missing} is not on PATH"
    )


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
    effort: str | None = None,
    agent: str | None,
    timeout: int,
    scaffold: bool,
    echo: bool = False,
    lessons_cap: int = LESSONS_CAP_LINES,
    lessons_cap_bytes: int = LESSONS_CAP_BYTES,
    continue_conversation: bool = False,
    permissions: str | None = None,
    allowed_tools: str | None = None,
    session: str | None = None,
    env: dict[str, str] | None = None,
    charge_hook: Callable[[], None] | None = None,
    max_credits: int | None = None,
    record: dict | None = None,
    memory: str = "off",
    memory_home: str | None = None,
    memory_writer: str | None = None,
    memory_people: str | None = None,
    memory_rig_context: dict | None = None,
    git_name: str | None = None,
    git_email: str | None = None,
    resident: bool = False,
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
    dispatched turn's budget draws. `record`, when given, collects what the
    turn measured about itself (`spend`, `otel`) for a caller with a
    machine-readable surface; the one-line human forms go to stderr either
    way. A roster turn arms the same instruments through the same helper —
    see `dispatch.run_harness`. `git_name` / `git_email` put the agent's git
    identity on the child's environment, over any inherited value (see
    `git_identity_env`); a bad value is refused before anything is touched.
    With the scaffold on and an `agent` named, a turn whose engine takes the
    a8s MCP server by a flag (`RUN_MCP_IDIOMS`) gets it spliced in and is
    taught `a8s_tell`; every other engine is taught the shell `tell`. A scaffolded turn whose lessons
    are due for consolidation saves its copies first
    (`prepare_lessons_copies`)."""
    identity = git_identity_env(git_name, git_email)
    if resident:
        from engines import resident as resident_mod

        template, note = _build_argv_template(
            engine,
            model=model,
            effort=effort,
            timeout=timeout,
            workdir=dir_path,
            continue_conversation=continue_conversation,
            permissions=permissions,
            allowed_tools=allowed_tools,
            session=session,
            max_credits=max_credits,
            resident=True,
        )
        if note:
            # The one-shot note names exec's flag; a resident session rewrites
            # it, so the note says what the session really runs with.
            note = resident_mod.RESIDENT_PERMISSION_NOTES.get((engine, permissions), note)
            print(f"r4t engine: {note}", file=sys.stderr)
        # Composed here as well as in the bridge, so a template the resident
        # adapter refuses fails this call rather than the detached session.
        composed = resident_mod.compose_resident_argv(
            template,
            dict(env if env is not None else os.environ),
            engine=engine,
        )
        if echo:
            boot_preview = (
                resident_mod.resident_boot_prompt(
                    dir_path,
                    message,
                    agent=agent,
                    engine=engine,
                    resident_seconds=timeout,
                    lessons_cap=lessons_cap,
                    lessons_cap_bytes=lessons_cap_bytes,
                )
                if scaffold
                else message
            )
            _print_echo(composed, boot_preview, identity)
        child_env = dict(env if env is not None else os.environ)
        child_env.update(identity)
        return resident_mod.enqueue_or_start(
            engine,
            message,
            dir_path=dir_path,
            template=template,
            model=model,
            effort=effort,
            agent=agent,
            timeout=timeout,
            scaffold=scaffold,
            lessons_cap=lessons_cap,
            lessons_cap_bytes=lessons_cap_bytes,
            env=child_env,
            charge_hook=charge_hook,
            memory=memory,
            memory_home=memory_home,
            memory_writer=memory_writer,
            memory_people=memory_people,
            memory_rig_context=memory_rig_context,
        )
    use_mcp = bool(
        scaffold and agent and HARNESS_PRESETS[engine].get("mcp") in RUN_MCP_IDIOMS
    )
    if scaffold:
        caps = {"lessons_cap": lessons_cap, "lessons_cap_bytes": lessons_cap_bytes}
        prompt = scaffold_prompt(
            dir_path,
            message,
            agent=agent,
            tell_step=mcp_tell_step(engine) if use_mcp else CLI_TELL_STEP,
            copies=prepare_lessons_copies(dir_path, **caps),
            **caps,
        )
    else:
        prompt = message
    memory_turn = None
    if memory and memory != "off":
        import engine_memory
        try:
            memory_turn = engine_memory.Turn(
                engine=engine, model=model, effort=effort, agent=agent,
                directory=dir_path, message=message, mode=memory,
                home=memory_home, writer=memory_writer, people=memory_people, env=env,
                rig_context=memory_rig_context,
            )
            prompt = memory_turn.inject(prompt)
        except ValueError as exc:
            raise RunError(str(exc)) from exc
        except OSError as exc:
            engine_memory.note(f"memory unavailable: {exc}")
            memory_turn = None
    with ExitStack() as stack:
        # A roster turn puts its instruments in the member's workdir, because
        # that is the one directory writable across an isolation boundary.
        # Nothing isolates a bare `engine run`, so it uses scratch of its own
        # and leaves the caller's directory alone.
        kind = HARNESS_PRESETS[engine].get("instruments")
        scratch = (
            Path(stack.enter_context(TemporaryDirectory(prefix="r4t-copilot-")))
            if kind
            else dir_path
        )
        instruments = stack.enter_context(
            copilot_engine.turn_instruments(
                kind, scratch=scratch, max_credits=max_credits
            )
        )
        template, note = _build_argv_template(
            engine,
            model=model,
            effort=effort,
            timeout=timeout,
            workdir=dir_path,
            continue_conversation=continue_conversation,
            permissions=permissions,
            allowed_tools=allowed_tools,
            session=session,
            instruments=instruments,
            max_credits=max_credits,
        )
        if note:
            print(f"r4t engine: {note}", file=sys.stderr)
        # The wake's routing facts belong to this turn; a nested `r4t engine run`
        # must key memory on its own --agent, not inherit this node's store.
        env = {k: v for k, v in (env if env is not None else os.environ).items()
               if not k.startswith("A8S_TURN_")}
        env.update(identity)
        if use_mcp:
            try:
                template = apply_mcp(Rig(name=engine, preset=engine), template, env, dir_path).argv
            except RigError as exc:
                raise RunError(str(exc)) from exc
        if echo:
            _print_echo(template, prompt, identity)
        argv = [prompt if a == "{prompt}" else a for a in template]
        if charge_hook is not None:
            charge_hook()
        if memory_turn is None:
            exit_code = _spawn(argv, dir_path, timeout, instruments.env_for(env))
        else:
            try:
                exit_code, output = engine_memory.spawn(argv, dir_path, timeout, instruments.env_for(env))
            except OSError as exc:
                try:
                    memory_turn.finish("", 1)
                except (OSError, ValueError) as capture_error:
                    engine_memory.note(f"failed turn capture unavailable: {capture_error}")
                raise RunError(f"failed to spawn {argv[0]!r}: {exc}") from exc
            try:
                memory_turn.finish(output, exit_code)
            except (OSError, ValueError) as exc:
                engine_memory.note(f"capture or worker failed: {exc}")
        if exit_code == 127:
            gap = interpreter_gap(argv[0], env.get("PATH"))
            if gap:
                print(f"r4t engine: exit 127: {gap}", file=sys.stderr)
        measured, lines = instruments.measure()
        for line in lines:
            print(f"r4t engine: {line}", file=sys.stderr)
        if record is not None:
            record.update(measured)
    return exit_code
