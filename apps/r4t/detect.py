"""`r4t rig detect` — the rig you already pay for, found instead of configured.

A stranger arrives with a Claude Code seat and a Copilot seat already bought
and logged in. Nothing here should ask him to name them: the machine knows,
and asking twice is the whole friction. Detection reads three things that
already exist and invents nothing —

- **is it installed** — `engines.check.check_engine`, the probe behind
  `r4t engine <id> check`. It composes the argv `run` would spend and asks the
  CLI whether it parses. A preset whose binary is absent is not detected; a
  preset whose binary is present but rejects the composed argv is detected as
  BROKEN rather than offered, because adding a rig that cannot run is worse
  than saying nothing.
- **what is left in it** — `engines.fuel`, the same reading `r4t rig fuel`
  reports, asked with no model pinned. Best-effort by construction: an engine
  with no quota surface, an expired login, or a dead endpoint costs the row
  its number and nothing else. **No turn is ever spent here.**
- **can it be added without asking** — a preset whose invoke carries an inline
  `{model}` (every `ollama-*` launcher) has no bare form, so `--add` skips it
  and says which flag it wanted.

Only `RUN_ENGINES` is probed. A preset r4t cannot drive headless is not a rig
a roster can hold, so detecting it would be an offer that fails at the first
turn.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import engines
from engines import check as engine_check
from engines.run import RUN_ENGINES
from rig import (
    HARNESS_PRESETS,
    RigError,
    add_preset_rig,
    build_preset_invoke,
    load_rig_config,
)

__all__ = [
    "GRADE_OFFICIAL",
    "GRADE_RUN_CAPABLE",
    "GRADE_NEEDS_MODEL",
    "Detection",
    "detect",
    "add_detected",
    "format_text",
]

# The grades a row can carry, weakest claim last. `official` is r4t's own
# supported-engine designation (docs/r4t-engine.md, "copilot, officially
# supported"): a model slot, a quota surface, a verified argv. `run-capable`
# is a verified headless invocation and no more. `needs --model` is a preset
# with no bare form at all.
GRADE_OFFICIAL = "official"
GRADE_RUN_CAPABLE = "run-capable"
GRADE_NEEDS_MODEL = "needs --model"

OFFICIAL_PRESETS = frozenset({"copilot"})


def preset_grade(preset: str) -> str:
    if any("{model}" in arg for arg in HARNESS_PRESETS[preset]["invoke"]):
        return GRADE_NEEDS_MODEL
    return GRADE_OFFICIAL if preset in OFFICIAL_PRESETS else GRADE_RUN_CAPABLE


@dataclass
class Detection:
    """One probed preset. `detected` is the only field that gates anything:
    installed AND its composed argv accepted."""

    preset: str
    engine: str
    grade: str
    installed: bool = False
    version: str | None = None
    verdict: str = engine_check.UNVERIFIABLE
    detail: str = ""
    fuel: float | None = None
    fuel_state: str | None = None
    fuel_note: str | None = None
    plan: str | None = None
    origin: str | None = None
    age_seconds: float | None = None

    @property
    def detected(self) -> bool:
        return self.installed and self.verdict == engine_check.ACCEPTED

    @property
    def addable(self) -> bool:
        return self.detected and self.grade != GRADE_NEEDS_MODEL

    @property
    def add_command(self) -> str:
        """The exact line that would create this rig. A `needs --model` preset
        shows the flag it is missing rather than a line that would fail."""
        base = f"r4t rig add {self.preset} {self.preset}"
        return f"{base} --model <model>" if self.grade == GRADE_NEEDS_MODEL else base

    def fuel_display(self) -> str:
        if isinstance(self.fuel, (int, float)):
            text = f"{round(self.fuel * 100)}%"
            if self.origin == "snapshot":
                text += f" ({engines.format_age(self.age_seconds)} old)"
            return text
        if self.fuel_state in ("unlimited", "unconstrained"):
            return "no gauge"
        return "n/a"

    def as_dict(self) -> dict:
        return {
            "preset": self.preset,
            "engine": self.engine,
            "grade": self.grade,
            "installed": self.installed,
            "version": self.version,
            "verdict": self.verdict,
            "detail": self.detail,
            "detected": self.detected,
            "addable": self.addable,
            "fuel": self.fuel,
            "fuel_state": self.fuel_state,
            "fuel_note": self.fuel_note,
            "plan": self.plan,
            "origin": self.origin,
            "age_seconds": self.age_seconds,
            "add_command": self.add_command,
        }


def _read_fuel(row: Detection, fuel_fn) -> None:
    """Ask the engine what is left, best-effort. A quota surface that cannot
    answer costs the row its number, never the detection."""
    try:
        payload = fuel_fn(row.preset, None)
    except engines.QuotaError as exc:
        row.fuel_note = str(exc)
        return
    except Exception as exc:  # a vendor endpoint is not a trusted boundary
        row.fuel_note = f"{type(exc).__name__}: {exc}"
        return
    row.fuel = payload.get("fuel")
    row.fuel_state = payload.get("state")
    row.plan = payload.get("plan")
    row.origin = payload.get("origin")
    row.age_seconds = payload.get("age_seconds")
    if payload.get("note"):
        row.fuel_note = str(payload["note"])


def detect(*, check_fn=None, fuel_fn=None, workdir: Path | None = None) -> list[Detection]:
    """Probe every run-capable preset and return one row each, sorted with the
    detected ones first. The two probes are injected so tests can drive the
    table without a harness or a network."""
    check_fn = check_fn or engine_check.check_engine
    fuel_fn = fuel_fn or engines.fuel
    rows: list[Detection] = []
    for preset in sorted(RUN_ENGINES):
        report = check_fn(preset, workdir=workdir)
        row = Detection(
            preset=preset,
            engine=engines.engine_for(preset) or preset,
            grade=preset_grade(preset),
            installed=report.installed,
            version=report.version,
            verdict=report.verdict,
            detail=report.detail,
        )
        if row.detected:
            _read_fuel(row, fuel_fn)
        rows.append(row)
    rows.sort(key=lambda r: (not r.detected, r.preset))
    return rows


def add_detected(path: Path, rows: list[Detection]) -> list[tuple[str, str, str]]:
    """Create a rig per addable detection, named after its preset. Returns
    (preset, outcome, detail) with outcome in added / exists / skipped.

    Idempotent by reading the config first: a rig that is already there is
    reported, never replaced, because a re-run must not overwrite a name the
    person has since tuned."""
    config = load_rig_config(path)
    existing = set() if config.missing else set(config.rigs)
    results: list[tuple[str, str, str]] = []
    for row in rows:
        if not row.detected:
            continue
        if row.grade == GRADE_NEEDS_MODEL:
            results.append((
                row.preset,
                "skipped",
                f"{row.preset} runs a local model and has no default — "
                f"add it yourself: {row.add_command}",
            ))
            continue
        if row.preset in existing:
            results.append((row.preset, "exists", f"rig {row.preset!r} is already in {path}"))
            continue
        try:
            add_preset_rig(path, row.preset, row.preset)
        except RigError as exc:
            results.append((row.preset, "skipped", str(exc)))
            continue
        existing.add(row.preset)
        invoke = " ".join(build_preset_invoke(row.preset))
        results.append((row.preset, "added", invoke))
    return results


def install_hint(rows: list[Detection]) -> str:
    """What to install when nothing was found. Named binaries, not vendors —
    the reader has to type one of them."""
    broken = [r for r in rows if r.installed and r.verdict == engine_check.REJECTED]
    if broken:
        names = ", ".join(f"{r.preset} ({r.detail})" for r in broken)
        return (
            f"No usable engine: {names}. Update the CLI, then re-run "
            f"`r4t rig detect` (details: r4t engine <id> check)."
        )
    return (
        "No agent CLI found on PATH. Install one — claude (Claude Code), "
        "copilot (GitHub Copilot CLI), codex, agent (Cursor), opencode, agy "
        "(Antigravity) or muse — then re-run `r4t rig detect`."
    )


def format_text(rows: list[Detection]) -> str:
    """The table, detected engines only, plus one line naming the rest."""
    found = [r for r in rows if r.detected]
    lines: list[str] = []
    if found:
        columns = [
            ("PRESET", lambda r: r.preset),
            ("ENGINE", lambda r: r.engine),
            ("INSTALLED", lambda r: r.version or "version unknown"),
            ("FUEL", lambda r: r.fuel_display()),
            ("GRADE", lambda r: r.grade),
            ("ADD IT WITH", lambda r: r.add_command),
        ]
        widths = [
            max(len(head), max(len(get(r)) for r in found))
            for head, get in columns
        ]
        header = "  ".join(h.ljust(w) for (h, _g), w in zip(columns, widths))
        lines.append("  " + header.rstrip())
        for r in found:
            cells = "  ".join(get(r).ljust(w) for (_h, get), w in zip(columns, widths))
            lines.append("  " + cells.rstrip())
            if r.plan:
                lines.append(f"    plan: {r.plan}")
            if r.fuel_note:
                lines.append(f"    note: {r.fuel_note}")
    missing = [r for r in rows if not r.detected]
    if missing:
        if found:
            lines.append("")
        lines.append("  not detected: " + ", ".join(
            f"{r.preset} ({r.detail or r.verdict})" for r in missing
        ))
    return "\n".join(lines)
