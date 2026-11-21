"""Option validation and working-directory configuration (`music.json`) for tool-music."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from ar3.fsio import atomic_write_text, read_text_if_present
from ar3.locking import file_lock

from .pins import MODEL_NAMES

CONFIG_FILENAME = "music.json"
LOCK_FILENAME = ".music.lock"
ENGINES = ("ace",)
FORMATS = ("wav", "flac", "m4a")
CONFIG_KEYS = ("engine", "model", "duration", "format", "steps")
MIN_DURATION = 10.0
MAX_DURATION = 600.0
DEFAULT_DURATION = 60.0
MAX_CAPTION_CHARS = 512
_KEY_RE = re.compile(r"^\s*([A-Ga-g])\s*([#b♯♭]?)\s*(major|minor|maj|min|m)?\s*$", re.IGNORECASE)


class MusicConfigError(ValueError):
    """Raised when an option value or `music.json` is malformed."""


def config_path(workdir: Path) -> Path:
    return Path(workdir).resolve() / CONFIG_FILENAME


def _lock_path(workdir: Path) -> Path:
    return Path(workdir).resolve() / LOCK_FILENAME


def validate_engine(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value not in ENGINES:
        raise MusicConfigError(f"unknown engine {raw!r} (choose from: {', '.join(ENGINES)})")
    return value


def validate_model(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value not in MODEL_NAMES:
        raise MusicConfigError(f"unknown model {raw!r} (choose from: {', '.join(MODEL_NAMES)})")
    return value


def validate_format(raw: str) -> str:
    value = (raw or "").strip().lower().lstrip(".")
    if value not in FORMATS:
        raise MusicConfigError(f"unsupported format {raw!r} (choose from: {', '.join(FORMATS)})")
    return value


def validate_duration(raw: str | float | int) -> float:
    try:
        value = float(str(raw).strip())
    except ValueError:
        raise MusicConfigError(f"invalid duration {raw!r} (expected seconds)") from None
    if not math.isfinite(value) or not MIN_DURATION <= value <= MAX_DURATION:
        raise MusicConfigError(
            f"duration must be between {MIN_DURATION:.0f} and {MAX_DURATION:.0f} seconds"
        )
    return round(value, 2)


def _bounded_int(raw: str | int, name: str, low: int, high: int) -> int:
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise MusicConfigError(f"invalid {name} {raw!r} (expected a whole number)") from None
    if not low <= value <= high:
        raise MusicConfigError(f"{name} must be between {low} and {high}")
    return value


def validate_steps(raw: str | int) -> int:
    return _bounded_int(raw, "steps", 1, 200)


def validate_bpm(raw: str | int) -> int:
    return _bounded_int(raw, "bpm", 30, 300)


def validate_seed(raw: str | int) -> int:
    return _bounded_int(raw, "seed", 0, 2**32 - 1)


def validate_key(raw: str) -> str:
    """Normalize `C`, `c#m`, `Bb minor`, `F# major` to ACE-Step's `<Note><accidental> <mode>`."""
    match = _KEY_RE.match(raw or "")
    if not match:
        raise MusicConfigError(f"invalid key {raw!r} (examples: 'C major', 'F# minor', 'Am')")
    note, accidental, mode = match.groups()
    accidental = {"♯": "#", "♭": "b"}.get(accidental, accidental)
    mode = "minor" if (mode or "").lower() in ("minor", "min", "m") else "major"
    return f"{note.upper()}{accidental} {mode}"


def validate_caption(text: str) -> str:
    caption = " ".join((text or "").split())
    if not caption:
        raise MusicConfigError("describe the music in the message body")
    if len(caption) > MAX_CAPTION_CHARS:
        raise MusicConfigError(
            f"the description is {len(caption)} characters; ACE-Step accepts at most {MAX_CAPTION_CHARS}"
        )
    return caption


_VALIDATORS = {
    "engine": validate_engine,
    "model": validate_model,
    "duration": validate_duration,
    "format": validate_format,
    "steps": validate_steps,
}


def read_config(workdir: Path) -> dict:
    """Read `<workdir>/music.json` (`{}` when absent)."""
    target = config_path(workdir)
    try:
        raw = read_text_if_present(target)
    except (OSError, UnicodeDecodeError) as exc:
        raise MusicConfigError(f"cannot read {target}: {exc}") from None
    if raw is None or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise MusicConfigError(f"invalid JSON in {target}: {exc}") from None
    if not isinstance(data, dict):
        raise MusicConfigError(f"{target} must contain a JSON object")
    return {key: _VALIDATORS[key](data[key]) for key in CONFIG_KEYS if data.get(key) is not None}


def update_config(workdir: Path, values: dict, unset_keys: tuple[str, ...] | list[str] = ()) -> dict:
    """Atomically update `<workdir>/music.json` and return the saved settings."""
    target = config_path(workdir)
    with file_lock(_lock_path(workdir)):
        current = read_config(workdir)
        for key in unset_keys:
            norm = key.strip().lower()
            if norm not in CONFIG_KEYS:
                raise MusicConfigError(f"unknown config key {key!r} (choose from: {', '.join(CONFIG_KEYS)})")
            current.pop(norm, None)
        for key, value in values.items():
            if value is not None:
                current[key] = _VALIDATORS[key](value)
        if current:
            atomic_write_text(target, json.dumps(current, indent=2, sort_keys=True) + "\n")
        else:
            target.unlink(missing_ok=True)
        return dict(current)


def clear_config(workdir: Path) -> bool:
    target = config_path(workdir)
    with file_lock(_lock_path(workdir)):
        existed = target.is_file()
        target.unlink(missing_ok=True)
        return existed
