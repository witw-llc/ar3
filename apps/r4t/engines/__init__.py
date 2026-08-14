"""Engine components — one module per agent CLI the suite can drive.

A rig preset says how to *invoke* an engine; an engine component is the one
place that knows how to *talk to* it for everything else. First verb: quota —
"how much subscription is left, and when does it reset" — answered without
spending a turn. The research behind each check is graded on the wiki
(Engine pages, section 13).

Every check returns the same shape so callers can route on it:

    {
      "engine": "codex",
      "origin": "live" | "snapshot",
      "plan": "team" | None,
      "buckets": [
        {"label": "Weekly Limit", "remaining_fraction": 0.24,
         "reset_time": "2026-08-12T18:55:34+00:00"},
      ],
      "note": None | str,        # context a fraction cannot carry
    }

`remaining_fraction` is 0.0-1.0 or None when the account cannot express one
(unlimited seats, unknown pools). A check that cannot answer raises
QuotaError with remediation in the message; `quota()` turns the last good
snapshot into an aged answer before giving up.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from engines import agy, claude, codex, copilot, cursor, ollama, opencode, run
from engines.base import QuotaError

__all__ = ["QuotaError", "MODULES", "engine_for", "quota", "format_text"]

MODULES = {
    "claude": claude,
    "codex": codex,
    "copilot": copilot,
    "cursor": cursor,
    "agy": agy,
    "opencode": opencode,
    "ollama": ollama,
}

# Preset ids that are not themselves engine names. Every `X-ollama` launcher
# preset runs local models — the quota authority is ollama (none), not the
# harness being launched.
PRESET_ENGINES = {
    "ollama-claude": "ollama",
    "ollama-codex": "ollama",
    "ollama-copilot": "ollama",
    "ollama-opencode": "ollama",
}


def engine_for(preset_or_engine: str) -> str | None:
    """The engine id behind a preset id (or the id itself), else None."""
    key = (preset_or_engine or "").strip().lower()
    if key in MODULES:
        return key
    return PRESET_ENGINES.get(key)


# The verbs an engine component may implement. A module implements a verb by
# defining a function of that name; internal callers ask `capability` and get
# None when the engine cannot answer, so dispatch can consult a live signal
# where one exists and stay on its static limits where one does not. The CLI
# is just another caller — renaming its spelling never touches this contract.
CAPABILITY_VERBS = ("quota",)


def capability(preset_or_engine: str, verb: str):
    """The callable implementing `verb` for this engine, or None."""
    engine = engine_for(preset_or_engine)
    if engine is None or verb not in CAPABILITY_VERBS:
        return None
    return getattr(MODULES[engine], verb, None)


def capabilities(preset_or_engine: str) -> list[str]:
    """The verbs `r4t engine <id>` answers. `quota` dispatches through the
    per-engine module (`capability` above); `run` is not — it is one shared
    implementation (engines/run.py) gated on RUN_ENGINES, the subset with a
    verified headless, unattended invocation (engine CLI fact sheet).
    RUN_ENGINES holds preset ids directly rather than quota-engine ids, so
    this checks the id itself instead of routing it through `engine_for` —
    the four `ollama-*` launchers each have their own run entry even though
    `engine_for` collapses all of them (and bare `ollama`) to the one quota
    engine `ollama`."""
    verbs = [v for v in CAPABILITY_VERBS if capability(preset_or_engine, v)]
    if (preset_or_engine or "").strip().lower() in run.RUN_ENGINES:
        verbs.append("run")
    return verbs


def snapshot_path(engine: str) -> Path:
    return Path.home() / ".config" / "r4t" / "quota" / f"{engine}.json"


def save_snapshot(engine: str, payload: dict) -> None:
    path = snapshot_path(engine)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"saved_at": time.time(), "payload": payload}),
        encoding="utf-8",
    )
    tmp.replace(path)


def load_snapshot(engine: str) -> dict | None:
    """The last good answer, re-labeled with its age, else None."""
    try:
        data = json.loads(snapshot_path(engine).read_text(encoding="utf-8"))
        saved_at = float(data["saved_at"])
        payload = dict(data["payload"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    mins = max(0, round((time.time() - saved_at) / 60))
    hours, mins = divmod(mins, 60)
    days, hours = divmod(hours, 24)
    payload["origin"] = "snapshot"
    payload["age"] = (
        f"{days}d {hours}h" if days else f"{hours}h {mins}m" if hours else f"{mins}m"
    )
    return payload


def quota(preset_or_engine: str) -> dict:
    """Live check for the engine behind `preset_or_engine`, falling back to
    the last snapshot. Raises QuotaError when neither can answer."""
    engine = engine_for(preset_or_engine)
    if engine is None:
        raise QuotaError(
            f"unknown engine or preset '{preset_or_engine}' "
            f"(engines: {', '.join(sorted(MODULES))})"
        )
    try:
        payload = MODULES[engine].quota()
    except QuotaError as exc:
        snapshot = load_snapshot(engine)
        if snapshot is None:
            raise
        snapshot["note"] = f"live check failed: {exc}"
        return snapshot
    payload["engine"] = engine
    if payload.get("origin") == "live":
        save_snapshot(engine, payload)
    return payload


def format_text(payload: dict) -> str:
    """Human lines for one engine's answer."""
    head = payload.get("engine", "?")
    plan = payload.get("plan")
    if plan:
        head += f" — {plan}"
    lines = [head]
    if payload.get("origin") == "snapshot":
        lines.append(f"  source: snapshot from {payload.get('age', '?')} ago")
    for bucket in payload.get("buckets") or []:
        fraction = bucket.get("remaining_fraction")
        if isinstance(fraction, (int, float)):
            value = f"{round(fraction * 100)}% remaining"
        else:
            value = "unknown"
        reset = bucket.get("reset_time")
        if reset:
            value += f" · resets {reset}"
        lines.append(f"  {bucket.get('label', 'Quota')}: {value}")
    if not payload.get("buckets"):
        lines.append("  no buckets reported")
    if payload.get("note"):
        lines.append(f"  note: {payload['note']}")
    return "\n".join(lines)
