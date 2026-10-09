"""What `--model` and `--effort` accept, per engine, without spending a turn.

Effort vocabularies are static facts in the preset table (`effort_values`);
model lists live in each CLI, so the live ones are read from the CLI's own
list verb. Both questions answer `(values, note)`: `values` may be empty when
the provider decides, and `note` then says so. A preset that cannot answer at
all raises ValuesError.
"""
from __future__ import annotations

import re
import subprocess

from rig import HARNESS_PRESETS, RigError, apply_effort

LIST_TIMEOUT_SECONDS = 30
CLAUDE_ALIASES = ["fable", "opus", "sonnet"]
CLAUDE_NOTE = "claude also accepts a full model name"

NO_LIST_VERB = {
    "codex": "`-m` is passed through and a bad id fails at spawn with codex's own message",
    "copilot": "`--model` is passed through and a bad id fails at spawn with copilot's own message",
    "opencode": "`-m` is passed through as provider/model and a bad id fails at spawn with opencode's own message",
    "muse": "`--model` is a model id for non-echo providers, passed through and checked by muse at spawn",
}


class ValuesError(Exception):
    """The preset cannot list the values asked for. The message says why."""


def _preset(preset: str) -> tuple[str, dict]:
    key = (preset or "").strip().lower()
    if key not in HARNESS_PRESETS:
        raise ValuesError(
            f"{preset!r} is not an engine or preset id "
            f"(see `r4t engine list`)"
        )
    return key, HARNESS_PRESETS[key]


def efforts(preset: str) -> tuple[list[str], str | None]:
    key, entry = _preset(preset)
    if entry.get("effort_values"):
        return list(entry["effort_values"]), None
    if entry.get("effort_argv"):
        return [], (
            f"engine {key!r} takes any non-empty effort value; "
            f"the provider decides (for example low, medium, high)"
        )
    try:
        apply_effort([], key, "x")
    except RigError as exc:
        raise ValuesError(str(exc)) from exc
    raise ValuesError(f"engine {key!r} does not support --effort")


def _lines(argv: list[str]) -> list[str]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=LIST_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ValuesError(f"could not run `{' '.join(argv)}`: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValuesError(
            f"`{' '.join(argv)}` timed out after {LIST_TIMEOUT_SECONDS}s"
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ValuesError(
            f"`{' '.join(argv)}` failed (exit {proc.returncode}): {detail}"
        )
    return proc.stdout.splitlines()


def _claude_models() -> tuple[list[str], str | None]:
    try:
        text = "\n".join(_lines(["claude", "--help"]))
    except ValuesError:
        return list(CLAUDE_ALIASES), CLAUDE_NOTE
    block = re.search(r"--model <model>(.*?)(?=\n\s*-)", text, re.S)
    found = re.findall(r"'([A-Za-z][\w.\-]*)'", block.group(1)) if block else []
    return (found or list(CLAUDE_ALIASES)), CLAUDE_NOTE


def _agy_models() -> list[str]:
    return [
        line.split("\t", 1)[0].strip()
        for line in _lines(["agy", "models"])
        if "\t" in line and line.split("\t", 1)[0].strip()
    ]


def _cursor_models(binary: str) -> list[str]:
    return [
        line.split(" - ", 1)[0].strip()
        for line in _lines([binary, "models"])
        if " - " in line
    ]


def _devin_models() -> list[str]:
    ids = []
    for line in _lines(["devin", "models", "list"]):
        match = re.match(r"^  (?!aliases:)(\S+)\s{2,}\S", line)
        if match:
            ids.append(match.group(1))
    return ids


def _ollama_models() -> list[str]:
    rows = _lines(["ollama", "list"])
    return [row.split()[0] for row in rows[1:] if row.strip()]


def has_models(preset: str) -> bool:
    key = (preset or "").strip().lower()
    return key in HARNESS_PRESETS and key not in NO_LIST_VERB


def models(preset: str) -> tuple[list[str], str | None]:
    key, entry = _preset(preset)
    if key in NO_LIST_VERB:
        raise ValuesError(f"{key} has no list verb; {NO_LIST_VERB[key]}")
    if key == "claude":
        return _claude_models()
    if key == "agy":
        values = _agy_models()
    elif key == "cursor":
        values = _cursor_models(entry["invoke"][0])
    elif key == "devin":
        values = _devin_models()
    else:
        values = _ollama_models()
    if not values:
        raise ValuesError(f"the {key} CLI listed no models")
    return values, None


def verbs(preset: str) -> list[str]:
    key = (preset or "").strip().lower()
    entry = HARNESS_PRESETS.get(key, {})
    return (["efforts"] if entry.get("effort_argv") else []) + (
        ["models"] if has_models(key) else []
    )
