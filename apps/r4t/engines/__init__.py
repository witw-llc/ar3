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

`fuel()` reduces that spread of dials to one number for one model. It is a
selection over the bucket shape above and nothing more — no engine's endpoint
is visible from here, so endpoint churn stays inside the engine modules. Its
`fuel` is the rig-level answer; the bucket-level readings it selects among
keep their own name, `remaining_fraction`.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from engines import agy, claude, codex, copilot, cursor, ollama, opencode, run
from engines.base import QuotaError

__all__ = [
    "QuotaError",
    "MODULES",
    "engine_for",
    "quota",
    "fuel",
    "binding_index",
    "format_age",
    "format_text",
]

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
    per-engine module (`capability` above); `run` and `check` are not — they
    are shared implementations (engines/run.py, engines/check.py) gated on
    RUN_ENGINES, the subset with a verified headless, unattended invocation
    (engine CLI fact sheet). `check` rides RUN_ENGINES because what it probes
    is exactly the argv `run` composes.
    RUN_ENGINES holds preset ids directly rather than quota-engine ids, so
    this checks the id itself instead of routing it through `engine_for` —
    the four `ollama-*` launchers each have their own run entry even though
    `engine_for` collapses all of them (and bare `ollama`) to the one quota
    engine `ollama`."""
    verbs = [v for v in CAPABILITY_VERBS if capability(preset_or_engine, v)]
    if (preset_or_engine or "").strip().lower() in run.RUN_ENGINES:
        verbs += ["run", "check"]
    return verbs


# How stale a snapshot may be and still be served after a live check fails.
# One of two independent guards, and it applies to every snapshot. It is not
# a proof that no reset was missed — age cannot see a boundary crossing, since
# a snapshot taken a minute before a reset is a minute old and already wrong,
# which is what `reset_passed` below answers. This bound answers the other
# question: a reset still ahead says the window did not turn over, but it says
# nothing about how much of the window was spent in the hours since the
# reading. A seat once served `0% remaining · resets 2026-08-18` on 2026-08-25
# — a week past the reset it named — because nothing here bounded the age.
SNAPSHOT_MAX_AGE_SECONDS = 4 * 3600

# How far into the future a snapshot may be dated before its age stops meaning
# anything. A clock slewing under NTP moves by fractions of a second, and an
# age of -0.2s is the same snapshot as one of +0.2s; a clock that has been
# *stepped* moves by minutes or hours, and then the age bound above is
# measuring nothing.
SNAPSHOT_FUTURE_TOLERANCE_SECONDS = 60


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
    payload["origin"] = "snapshot"
    # Not clamped at zero: a saved_at in the future means the clock moved, and
    # a clamp would hide that behind an age of nothing. Renderers clamp for
    # display; the policy in `quota` needs to see the real sign.
    payload["age_seconds"] = round(time.time() - saved_at, 3)
    return payload


def reset_passed(payload: dict, now: float | None = None) -> tuple[str, str] | None:
    """The first bucket whose stated reset is already behind us, as
    (label, reset_time). Such a payload is quoting numbers that no longer
    exist however young it is — the reset is the fact, the age is a proxy."""
    at = time.time() if now is None else now
    for bucket in payload.get("buckets") or []:
        reset = bucket.get("reset_time")
        if not isinstance(reset, str) or not reset.strip():
            continue
        try:
            when = datetime.fromisoformat(reset.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when.timestamp() <= at:
            return str(bucket.get("label") or "Quota"), reset
    return None


def format_age(seconds: float | None) -> str:
    """A duration in seconds as the one human string the renderers print.
    Machine surfaces carry `age_seconds` and nothing else."""
    if not isinstance(seconds, (int, float)):
        return "?"
    mins = max(0, round(seconds / 60))
    hours, mins = divmod(mins, 60)
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if days else f"{hours}h {mins}m" if hours else f"{mins}m"


def quota(preset_or_engine: str) -> dict:
    """Live check for the engine behind `preset_or_engine`, falling back to a
    snapshot young enough to still be true. Raises QuotaError when neither can
    answer."""
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
        crossed = reset_passed(snapshot)
        if crossed is not None:
            label, reset = crossed
            raise QuotaError(
                f"{exc} — and the last snapshot's {label} reset at {reset}, "
                f"which has passed, so its numbers no longer exist"
            ) from exc
        age = snapshot.get("age_seconds")
        if isinstance(age, (int, float)) and age < -SNAPSHOT_FUTURE_TOLERANCE_SECONDS:
            raise QuotaError(
                f"{exc} — and the last snapshot is dated "
                f"{format_age(-age)} in the future, so its age proves nothing "
                f"about whether its numbers were reset"
            ) from exc
        if isinstance(age, (int, float)) and age > SNAPSHOT_MAX_AGE_SECONDS:
            raise QuotaError(
                f"{exc} — and the last snapshot is {format_age(age)} old, past "
                f"the {format_age(SNAPSHOT_MAX_AGE_SECONDS)} bound, so its "
                f"numbers may already have been reset"
            ) from exc
        snapshot["engine"] = engine
        snapshot["note"] = f"live check failed: {exc}"
        return snapshot
    payload["engine"] = engine
    if payload.get("origin") == "live":
        save_snapshot(engine, payload)
    return payload


# Which buckets constrain which models. A bucket whose label names a model
# family is a dial only that family turns — claude's model-scoped weeklies,
# antigravity's per-pool limits; every other bucket constrains everything the
# engine runs. The key is the token an engine writes into such a label, the
# value the tokens a model name carries when it belongs to that family.
SCOPED_BUCKETS = {
    "claude/gpt": ("claude", "opus", "sonnet", "haiku", "gpt", "codex"),
    "gemini": ("gemini",),
    "fable": ("fable",),
    "opus": ("opus",),
    "sonnet": ("sonnet",),
    "haiku": ("haiku",),
}


def _constrains(bucket: dict, model: str | None) -> bool:
    """Whether this bucket empties when `model` runs. An unscoped bucket
    always does. A scoped one does when the model belongs to ANY family its
    label names — the label is server-supplied text, and one bucket may be
    shared ("Sonnet & Opus shared weekly"), so the scopes union rather than
    the first one winning. A scoped bucket never constrains a rig that pins
    no model, since counting a Fable weekly against a rig that may not be
    running Fable reports an empty tank that is not empty."""
    label = str(bucket.get("label", "")).lower()
    families = [
        token
        for scope, models in SCOPED_BUCKETS.items()
        if scope in label
        for token in models
    ]
    if not families:
        return True
    return bool(model) and any(token in model.lower() for token in families)


def binding_index(buckets: list[dict]) -> int | None:
    """Which bucket runs out first: the lowest fraction among those that
    express one, ties going to the first declared. One rule, so the JSON's
    `binding_label` and the text renderer's star can never disagree — even
    when two buckets carry the same label."""
    gauged = [
        i
        for i, bucket in enumerate(buckets)
        if isinstance(bucket.get("remaining_fraction"), (int, float))
    ]
    if not gauged:
        return None
    return min(gauged, key=lambda i: buckets[i]["remaining_fraction"])


def fuel(preset_or_engine: str, model: str | None = None) -> dict:
    """How much tank is left for one model on one engine: the engine's quota,
    narrowed to the buckets that model burns, reduced to the binding one.

    `state` is what a dispatcher branches on, because `fuel` alone cannot tell
    an empty account from an unmeasured one:

      gauged         a bucket answered — `fuel` is 0.0-1.0 (a local engine's
                     1.0 included).
      unlimited      buckets constrain this model but none expresses a
                     fraction (an unlimited seat, an engine with no gauge).
      unconstrained  no bucket constrains this model at all — every dial the
                     account carries is scoped to some other family, or the
                     rig pins no model and every dial is scoped.

    `fuel` is None for the last two. Buckets that cannot express a fraction
    are dropped rather than counted as empty. `preset` is the id the caller
    asked for, the same value `rig run --json` calls `engine`; `quota_engine`
    is the engine that actually answered. Raises QuotaError exactly where
    `quota` does."""
    payload = quota(preset_or_engine)
    buckets = [b for b in payload.get("buckets") or [] if _constrains(b, model)]
    index = binding_index(buckets)
    binding = buckets[index] if index is not None else None
    return {
        "preset": preset_or_engine,
        "quota_engine": payload.get("engine"),
        "model": model,
        "fuel": binding["remaining_fraction"] if binding else None,
        "state": (
            "gauged" if binding else "unlimited" if buckets else "unconstrained"
        ),
        "binding_label": binding["label"] if binding else None,
        "binding_reset": binding.get("reset_time") if binding else None,
        "origin": payload.get("origin"),
        "age_seconds": payload.get("age_seconds"),
        "plan": payload.get("plan"),
        "buckets": buckets,
        "note": payload.get("note"),
    }


def format_text(payload: dict) -> str:
    """Human lines for one engine's answer."""
    head = payload.get("engine", "?")
    plan = payload.get("plan")
    if plan:
        head += f" — {plan}"
    lines = [head]
    if payload.get("origin") == "snapshot":
        lines.append(
            f"  source: snapshot from {format_age(payload.get('age_seconds'))} ago"
        )
    # Above the numbers, not below them. A caveat printed after the figures is
    # read after the reader has already believed them.
    if payload.get("note"):
        lines.append(f"  note: {payload['note']}")
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
    return "\n".join(lines)
