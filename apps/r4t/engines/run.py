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
    RigError,
    apply_allowed_tools,
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
    "LESSONS_SOFT_CAP",
    "LESSONS_ROTATE_MARKER",
    "LESSONS_ARCHIVE_NAME",
    "DEFAULT_IDLE_PROMPT",
    "RunError",
    "build_argv",
    "git_identity_env",
    "scaffold_prompt",
    "rotate_lessons_if_oversized",
    "prepare_lessons_fold",
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
# Fraction of either cap past which a turn is told to fold before it appends.
LESSONS_SOFT_CAP = 0.8
LESSONS_ROTATE_MARKER = "<!-- rotate-below -->"
LESSONS_ARCHIVE_NAME = "LESSONS-ARCHIVE.md"
LESSONS_FOLD_DIR = "archive"
TIMEOUT_EXIT_CODE = 124  # matches the `timeout(1)` convention

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
    lessons_cap: int = LESSONS_CAP_LINES,
    lessons_cap_bytes: int = LESSONS_CAP_BYTES,
    fold: tuple[Path, Path] | None = None,
) -> str:
    """The fixed cold-boot prelude, then this turn's LESSONS.md note, then the
    volatile `message` last. The prelude stays byte-identical across runs in
    the same `dir_path`, so the prompt cache only ever misses on what follows
    it. Its one conditional sentence, the archive pointer, turns on once in a
    seat's life: rotation creates the archive and r4t never removes it. The
    note changes turn to turn, so it sits after the prelude: the soft-cap
    nudge when LESSONS.md is over the soft cap, or on an idle turn the fold
    `prepare_lessons_fold` readied (`fold`), which replaces the nudge."""
    status = dir_path / "STATUS.md"
    agents_file = dir_path / "AGENTS.md"
    lessons = dir_path / "LESSONS.md"
    archive = dir_path / LESSONS_ARCHIVE_NAME
    read_step = (
        f"1. Read {status}, then {agents_file} and {lessons} if present. Use "
        "these absolute paths even if your workspace root differs. They are "
        "the durable source of truth; you have no transcript memory."
    )
    try:
        if archive.stat().st_size:
            read_step += f" Older lessons: {archive} (grep it; do not read it whole)."
    except OSError:
        pass
    steps = [read_step]
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
    note = _lessons_note(dir_path, lessons_cap, lessons_cap_bytes, fold)
    tail = f"\n\n{note}" if note else ""
    return f"{prelude}{tail}\n\nRouted input:\n{message}"


def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _lessons_text(data: bytes) -> str | None:
    """LESSONS.md as text with LF line endings, or None when it is not UTF-8.
    Every size r4t reports or caps counts LF endings, which is how rotation
    writes the file."""
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


def _lines_text(lines: list[str]) -> str:
    return "".join(f"{line}\n" for line in lines)


def _line_bytes(line: str) -> int:
    return len(line.encode("utf-8")) + 1


def _kb(size: int) -> str:
    text = f"{size / 1024:.1f}"
    return text[:-2] if text.endswith(".0") else text


def _over_soft_cap(lines: int, size: int, cap: int, cap_bytes: int) -> bool:
    return lines > cap * LESSONS_SOFT_CAP or size > cap_bytes * LESSONS_SOFT_CAP


def _heading_level(line: str) -> int:
    """The level of a markdown ATX heading (1-6), or 0 for any other line."""
    level = len(line) - len(line.lstrip("#"))
    if 1 <= level <= 6 and line[level:level + 1] in ("", " ", "\t"):
        return level
    return 0


def _pinned_count(lines: list[str]) -> int:
    """How many leading lines of LESSONS.md are its header, which never
    rotates: through the first `<!-- rotate-below -->` line when there is one,
    else every line above the first `## ` heading, else none."""
    for index, line in enumerate(lines):
        if line.strip() == LESSONS_ROTATE_MARKER:
            return index + 1
    for index, line in enumerate(lines):
        if _heading_level(line) == 2:
            return index
    return 0


def _push_heading(chain: list[tuple[int, str]], line: str) -> None:
    level = _heading_level(line)
    if level:
        while chain and chain[-1][0] >= level:
            chain.pop()
        chain.append((level, line))


def _carried(chain: list[tuple[int, str]], next_line: str | None) -> list[str]:
    """The moved headings that still govern `next_line`, outermost first:
    every open one before a plain line, only the shallower ones before a
    heading, none when nothing is left after the cut."""
    if next_line is None:
        return []
    level = _heading_level(next_line)
    return [line for depth, line in chain if not level or depth < level]


def _rotation_cut(
    body: list[str], head: list[str], cap: int, cap_bytes: int
) -> tuple[int, list[str]]:
    """How many leading `body` lines move, and the headings the live file
    repeats above the rest. The cut is the smallest one after which header,
    repeated headings and remaining body fit both caps, then pushed past any
    blank lines, which carry nothing and would open the kept text."""
    head_bytes = sum(map(_line_bytes, head))
    rest_bytes = sum(map(_line_bytes, body))
    chain: list[tuple[int, str]] = []
    cut = 0
    while cut < len(body):
        carry = _carried(chain, body[cut])
        if (
            len(head) + len(carry) + len(body) - cut <= cap
            and head_bytes + sum(map(_line_bytes, carry)) + rest_bytes <= cap_bytes
        ):
            break
        _push_heading(chain, body[cut])
        rest_bytes -= _line_bytes(body[cut])
        cut += 1
    while cut < len(body) and not body[cut].strip():
        cut += 1
    return cut, _carried(chain, body[cut] if cut < len(body) else None)


def rotate_lessons_if_oversized(
    dir_path: Path,
    cap: int = LESSONS_CAP_LINES,
    cap_bytes: int = LESSONS_CAP_BYTES,
) -> None:
    """Rotate, never merge; no model touches either file. A LESSONS.md over
    `cap` lines or `cap_bytes` bytes has its oldest lines moved out, whole
    lines only, until the live file fits both caps. The header never moves
    (`_pinned_count`). A cut inside a section repeats the headings still
    governing the kept lines at the top of the live file, and the archive
    keeps them in place, so neither file holds lines cut off from their
    heading.

    Moved lines are appended in order to LESSONS-ARCHIVE.md (created if
    absent) before LESSONS.md is rewritten, so a kill between the two writes
    can only duplicate lines into the archive, never lose them — each file's
    own write is atomic via temp file + os.replace, but the pair is not one
    transaction. Both files are written with LF line endings, whatever they
    held before, so neither ends up with mixed endings and the byte cap holds
    on disk. A header over a cap on its own is reported on stderr, since
    nothing below it can bring the file under. A missing or unreadable
    LESSONS.md is silently not-oversized; one that is not UTF-8 is left as it
    is, with a stderr line."""
    lessons_path = dir_path / "LESSONS.md"
    try:
        data = lessons_path.read_bytes()
    except OSError:
        return
    text = _lessons_text(data)
    if text is None:
        print(
            f"r4t engine: {lessons_path} is not UTF-8, so it is not rotated",
            file=sys.stderr,
        )
        return
    lines = _lessons_lines(text)
    if len(lines) <= cap and len(text.encode("utf-8")) <= cap_bytes:
        return
    pinned = _pinned_count(lines)
    head, body = lines[:pinned], lines[pinned:]
    cut, carried = _rotation_cut(body, head, cap, cap_bytes)

    if cut:
        archive_path = dir_path / LESSONS_ARCHIVE_NAME
        try:
            archive_prefix = _lf(archive_path.read_bytes())
        except OSError:
            archive_prefix = b""
        if archive_prefix and not archive_prefix.endswith(b"\n"):
            archive_prefix += b"\n"
        moved = _lines_text(body[:cut]).encode("utf-8")
        _atomic_write_bytes(archive_path, archive_prefix + moved)
        _atomic_write_bytes(
            lessons_path, _lines_text(head + carried + body[cut:]).encode("utf-8")
        )
        print(
            f"r4t engine: rotated {cut} lines from {lessons_path} to {archive_path}",
            file=sys.stderr,
        )

    head_bytes = sum(map(_line_bytes, head))
    if pinned > cap or head_bytes > cap_bytes:
        print(
            f"r4t engine: the header of {lessons_path} is over the cap on its "
            f"own ({pinned} lines, {head_bytes} bytes; cap {cap} lines, "
            f"{cap_bytes} bytes); put {LESSONS_ROTATE_MARKER} higher in it",
            file=sys.stderr,
        )


def _fold_day() -> str:
    """The fold's date, UTC like every date in a filename: it names files
    that sort, so it cannot move with the machine's zone."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def prepare_lessons_fold(
    dir_path: Path,
    *,
    lessons_cap: int = LESSONS_CAP_LINES,
    lessons_cap_bytes: int = LESSONS_CAP_BYTES,
) -> tuple[Path, Path] | None:
    """Ready an idle turn's fold of LESSONS.md. When the file is over the
    soft cap and no fold has started today, copy its bytes to
    archive/lessons-fold-<date>-source.md and return (that copy, the ledger
    path the turn writes). The copy keeps the fold lossless: a line the model
    merges or drops is still on disk, and the ledger's line numbers point into
    it. So the fold, the only turn permitted to edit an existing line, is
    granted only once the copy reads back equal to LESSONS.md. A copy is never
    overwritten, which also holds a seat to one fold a day. None when no fold
    is due or no verified copy exists; the turn then gets the append-only
    nudge."""
    lessons = dir_path / "LESSONS.md"
    try:
        data = lessons.read_bytes()
    except OSError:
        return None
    text = _lessons_text(data)
    if text is None or not _over_soft_cap(
        len(_lessons_lines(text)), len(text.encode("utf-8")),
        lessons_cap, lessons_cap_bytes,
    ):
        return None
    folds = dir_path / LESSONS_FOLD_DIR
    day = _fold_day()
    source = folds / f"lessons-fold-{day}-source.md"
    if source.exists():
        return None
    try:
        folds.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(source, data)
        verified = source.read_bytes() == data == lessons.read_bytes()
    except OSError as exc:
        print(f"r4t engine: no fold this turn: {exc}", file=sys.stderr)
        return None
    if not verified:
        print(
            f"r4t engine: no fold this turn: {source} does not match {lessons}",
            file=sys.stderr,
        )
        return None
    return source, folds / f"lessons-fold-{day}.md"


def _lessons_note(
    dir_path: Path, cap: int, cap_bytes: int, fold: tuple[Path, Path] | None
) -> str:
    """This turn's LESSONS.md note: the fold when one is readied, else the
    soft-cap nudge when the file is over the soft cap, else nothing. Only the
    fold permits editing an existing line, because only the fold has a copy to
    recover from; the nudge keeps the turn append-only."""
    lessons = dir_path / "LESSONS.md"
    text = _read_lessons(lessons)
    if text is None:
        return ""
    lines = _lessons_lines(text)
    size = len(text.encode("utf-8"))
    figures = (
        f"{lessons} is {len(lines)} lines / {_kb(size)} KB of {cap} lines / "
        f"{_kb(cap_bytes)} KB"
    )
    if fold is not None:
        source, ledger = fold
        rules = [
            "- Merge each duplicate into the lesson it repeats.",
            "- Drop a lesson only when a newer lesson supersedes it. Never "
            "drop a live rule.",
        ]
        pinned = _pinned_count(lines)
        if pinned:
            span = "line 1" if pinned == 1 else f"lines 1-{pinned}"
            rules.append(f"- Keep {span} (the header) unchanged.")
        rules.append(
            f"- Write {ledger} with one table row per line of the copy: "
            "source line number; kept, merged-into or dropped-superseded; "
            "target, the line of the new file that holds it, absorbed it or "
            "supersedes it."
        )
        return "\n".join([
            f"Idle fold: {figures}. Fold it in this pass. {source} is its "
            "exact copy from before this turn.",
            *rules,
            "This fold is the one rewrite of LESSONS.md that the append-only "
            "rule allows.",
        ])
    if _over_soft_cap(len(lines), size, cap, cap_bytes):
        return (
            f"{figures}. Do not append a lesson that repeats one already here; "
            "the idle pass consolidates. Do not edit existing lines."
        )
    return ""


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
    idle: bool = False,
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
    `idle` marks the turn as the quiet-tick pass: with the scaffold on and
    LESSONS.md over the soft cap, it carries the fold (`prepare_lessons_fold`)
    whatever its routed input says."""
    identity = git_identity_env(git_name, git_email)
    if scaffold:
        rotate_lessons_if_oversized(dir_path, lessons_cap, lessons_cap_bytes)
        caps = {"lessons_cap": lessons_cap, "lessons_cap_bytes": lessons_cap_bytes}
        fold = prepare_lessons_fold(dir_path, **caps) if idle else None
        prompt = scaffold_prompt(dir_path, message, agent=agent, fold=fold, **caps)
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
        if echo:
            _print_echo(template, prompt, identity)
        argv = [prompt if a == "{prompt}" else a for a in template]
        if charge_hook is not None:
            charge_hook()
        # The wake's routing facts belong to this turn; a nested `r4t engine run`
        # must key memory on its own --agent, not inherit this node's store.
        env = {k: v for k, v in (env if env is not None else os.environ).items()
               if not k.startswith("A8S_TURN_")}
        env.update(identity)
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
        measured, lines = instruments.measure()
        for line in lines:
            print(f"r4t engine: {line}", file=sys.stderr)
        if record is not None:
            record.update(measured)
    return exit_code
