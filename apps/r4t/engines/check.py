"""`r4t engine <id> check` — does the installed CLI accept the argv r4t
composes for it?

The suite spells each engine's idiom in the preset table, and the CLIs move
underneath it. Two shipped presets drifted at once and neither was caught by a
test: `codex exec --full-auto` stopped parsing (clap is strict, so it failed
loudly) and opencode's `--dangerously-skip-permissions` never existed at all
(its parser is lenient, so it failed silently, running turns with
auto-approval OFF). No test asserts that a composed argv is accepted by the
installed binary, which is the gap this verb closes.

**No tokens are spent.** A check never runs a turn: it drives the CLI's own
`--help` and `--version`, and nothing else. The prompt is removed from the
argv before any probe.

Two detection shapes, chosen per engine and recorded in PROBES with the reason:

- **parse probe** — clap (codex) reports an unexpected argument even when
  `--help` is present, so the composed argv goes to the CLI itself and its
  exit code is the verdict. The strongest signal available.
- **help scan** — every other CLI here short-circuits at `--help` before it
  validates the rest of argv, so the composed argv's long flags are matched
  against the flags the CLI's own help lists. Weaker, and the only thing that
  catches a lenient parser at all.

A missing binary is `unverifiable`, not a failure: a machine that has never
installed cursor is not a machine with a broken cursor preset.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from engines.run import RUN_ENGINES, RunError, _build_argv_template, resolve_argv0

__all__ = ["PROBES", "EngineReport", "check_engine", "check_all", "format_text"]

PROBE_TIMEOUT_SECONDS = 30
# `ollama launch` has no default model, so composing an ollama-* argv needs
# one. It is never executed — those engines are help-scanned — so the string
# only has to hold the argv's shape.
PROBE_MODEL = "MODEL"


@dataclass(frozen=True)
class Probe:
    """How one engine is checked. `help_binary`/`help_argv` produce the flag
    list; `strict` says the CLI itself can be handed the argv; `flags_after`
    scopes the scan to the tokens past a separator, for the `ollama launch`
    presets whose leading flags belong to the launcher rather than to the CLI
    being launched."""

    help_binary: str
    help_argv: tuple[str, ...]
    reason: str
    strict: bool = False
    flags_after: str | None = None


_CLAUDE_HELP = (
    "claude's --help prints and exits before it validates the rest of argv, "
    "so its flags are matched against the help text"
)
_CODEX_HELP = (
    "clap reports an unexpected argument even with --help present, so codex "
    "parses the composed argv itself"
)
_OPENCODE_HELP = (
    "opencode accepts unknown flags without complaint, so the help text is "
    "the only place an unknown flag shows up"
)

PROBES: dict[str, Probe] = {
    "claude": Probe("claude", ("--help",), _CLAUDE_HELP),
    "codex": Probe("codex", ("exec", "--help"), _CODEX_HELP, strict=True),
    "cursor": Probe(
        "agent", ("--help",),
        "cursor-agent's --help short-circuits the same way claude's does",
    ),
    "copilot": Probe(
        "copilot", ("--help",),
        "copilot's --help short-circuits before validating the rest of argv",
    ),
    "agy": Probe(
        "agy", ("--help",),
        "agy prints its Go flag listing and exits, so the listing is the check",
    ),
    "opencode": Probe("opencode", ("run", "--help"), _OPENCODE_HELP),
    # The launchers: `ollama launch <cli> ... -- <cli flags>`. Everything
    # before the separator is ollama's own; everything after is the CLI's, and
    # that is the half a preset gets wrong.
    "ollama-claude": Probe("claude", ("--help",), _CLAUDE_HELP, flags_after="--"),
    "ollama-codex": Probe(
        "codex", ("exec", "--help"),
        "the launcher is not run, so codex's help lists the flags to match",
        flags_after="--",
    ),
    "ollama-opencode": Probe(
        "opencode", ("run", "--help"), _OPENCODE_HELP, flags_after="--"
    ),
}

ACCEPTED = "accepted"
REJECTED = "rejected"
UNVERIFIABLE = "unverifiable"


@dataclass
class EngineReport:
    engine: str
    binary: str = ""
    version: str | None = None
    installed: bool = False
    verdict: str = UNVERIFIABLE
    detail: str = ""
    argv: list[str] = field(default_factory=list)
    method: str = ""

    def as_dict(self) -> dict:
        return {
            "engine": self.engine,
            "binary": self.binary,
            "version": self.version,
            "installed": self.installed,
            "verdict": self.verdict,
            "detail": self.detail,
            "method": self.method,
            "argv": self.argv,
        }


def _run(argv: list[str]) -> tuple[int, str]:
    """One probe process: stdin closed, output captured, hard timeout. A CLI
    that cannot answer leaves the caller with `unverifiable` rather than a
    hung check."""
    try:
        proc = subprocess.run(
            resolve_argv0(argv),
            capture_output=True,
            # Explicit decode: Windows' locale default is the ANSI code page
            # with strict errors, and a CLI's UTF-8 help text crashes it —
            # the decode-side failure class of the Windows research §4.6.
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _version(binary: str) -> str | None:
    code, out = _run([binary, "--version"])
    if code != 0:
        return None
    first = next((line.strip() for line in out.splitlines() if line.strip()), "")
    return first or None


def _long_flags(argv: list[str], after: str | None) -> list[str]:
    """The long flags in a composed argv, in order, without repeats. `--` is a
    separator rather than a flag, and a token like `--dir` is checked while its
    value is not."""
    tokens = argv
    if after is not None:
        tokens = argv[argv.index(after) + 1:] if after in argv else []
    flags: list[str] = []
    for token in tokens:
        if token.startswith("--") and len(token) > 2 and token not in flags:
            flags.append(token.split("=", 1)[0])
    return flags


def _mentions(help_text: str, flag: str) -> bool:
    """Whether the CLI's help lists `flag` as a flag of its own. The bounds
    matter: `--allow-all` must not match inside `--allow-all-tools`."""
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text) is not None


def check_engine(
    engine: str,
    *,
    model: str | None = None,
    permissions: str | None = None,
    allowed_tools: str | None = None,
    continue_conversation: bool = False,
    timeout: int = 900,
    workdir: Path | None = None,
) -> EngineReport:
    """Compose this engine's argv and ask the installed CLI whether it parses.
    The same flags `run` takes are accepted here, so a composition can be
    checked before it is spent."""
    report = EngineReport(engine=engine)
    if model is None and engine.startswith("ollama-"):
        model = PROBE_MODEL
    try:
        template, _ = _build_argv_template(
            engine,
            model=model,
            timeout=timeout,
            workdir=workdir or Path.cwd(),
            continue_conversation=continue_conversation,
            permissions=permissions,
            allowed_tools=allowed_tools,
        )
    except RunError as exc:
        # r4t refused to compose it at all, which is a rejection the caller
        # needs to see in the same table as a CLI's.
        report.verdict = REJECTED
        report.detail = str(exc)
        report.method = "composition"
        return report

    report.argv = template
    report.binary = template[0]
    probe = PROBES[engine]
    report.installed = shutil.which(report.binary) is not None
    if not report.installed:
        report.detail = f"{report.binary} is not on PATH"
        return report
    report.version = _version(report.binary)

    if probe.strict:
        report.method = "parse probe"
        code, out = _run([a for a in template if a != "{prompt}"] + ["--help"])
        if code != 0:
            report.verdict = REJECTED
            report.detail = next(
                (line.strip() for line in out.splitlines() if line.strip()),
                f"exit {code}",
            )
            return report
        report.verdict = ACCEPTED
        return report

    report.method = "help scan"
    if shutil.which(probe.help_binary) is None:
        report.detail = f"{probe.help_binary} is not on PATH to read its flags from"
        return report
    code, help_text = _run([probe.help_binary, *probe.help_argv])
    if code != 0 or not help_text.strip():
        report.detail = f"`{probe.help_binary} {' '.join(probe.help_argv)}` gave no help text"
        return report
    unknown = [
        flag
        for flag in _long_flags(template, probe.flags_after)
        if not _mentions(help_text, flag)
    ]
    if unknown:
        report.verdict = REJECTED
        report.detail = (
            f"{probe.help_binary} lists no {', '.join(unknown)}"
        )
        return report
    report.verdict = ACCEPTED
    return report


def check_all(**kwargs) -> list[EngineReport]:
    """Every run-capable engine, in one pass, sorted by id."""
    return [check_engine(engine, **kwargs) for engine in sorted(RUN_ENGINES)]


def format_text(reports: list[EngineReport]) -> str:
    width = max(len(r.engine) for r in reports)
    version_width = max(len(r.version or "not installed") for r in reports)
    lines = []
    for r in reports:
        version = r.version or ("version unknown" if r.installed else "not installed")
        detail = f"  {r.detail}" if r.detail else ""
        method = f" ({r.method})" if r.method and r.verdict == ACCEPTED else ""
        lines.append(
            f"  {r.engine:<{width}}  {version:<{version_width}}  "
            f"{r.verdict}{method}{detail}"
        )
    return "\n".join(lines)
