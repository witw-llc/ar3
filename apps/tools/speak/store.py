"""Working-directory configuration (`speak.json`), replacements, and pronunciations for tool-speak."""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

from ar3.fsio import atomic_write_text, read_text_if_present
from ar3.locking import file_lock

from .voices import ENGINE_CHOICES, ENGINES, parse_voice_entry, resolve_for_engine

CONFIG_FILENAME = "speak.json"
REPLACEMENTS_FILENAME = "replacements.txt"
PRONUNCIATIONS_FILENAME = "pronunciations.txt"
LOCK_FILENAME = ".speak.lock"
VALID_FORMATS = ("m4a", "mp3", "wav")
CONFIG_KEYS = ("engine", "voice", "model", "format", "speed", "style")


class SpeakConfigError(ValueError):
    """Raised when configuration, replacement, or pronunciation rules are malformed."""


def config_path(workdir: Path) -> Path:
    return Path(workdir).resolve() / CONFIG_FILENAME


def replacements_path(workdir: Path) -> Path:
    return Path(workdir).resolve() / REPLACEMENTS_FILENAME


def pronunciations_path(workdir: Path) -> Path:
    return Path(workdir).resolve() / PRONUNCIATIONS_FILENAME


def _lock_path(workdir: Path) -> Path:
    return Path(workdir).resolve() / LOCK_FILENAME


def validate_format(raw: str) -> str:
    fmt = (raw or "").strip().lower().lstrip(".")
    if fmt not in VALID_FORMATS:
        raise SpeakConfigError(
            f"unsupported format {raw!r} (choose from: {', '.join(VALID_FORMATS)})"
        )
    return fmt


def validate_speed(raw: str | float | int) -> float:
    try:
        val = float(str(raw).strip())
    except ValueError:
        raise SpeakConfigError(f"invalid speed {raw!r} (expected a positive number)") from None
    if not math.isfinite(val) or val <= 0:
        raise SpeakConfigError("speed must be a positive number")
    return round(val, 4)


def validate_engine_choice(raw: str) -> str:
    eng = (raw or "").strip().lower()
    if eng not in ENGINE_CHOICES:
        raise SpeakConfigError(
            f"unknown engine {raw!r} (choose from: {', '.join(ENGINE_CHOICES)})"
        )
    return eng


def read_config(workdir: Path) -> dict:
    """Read `<workdir>/speak.json` (`{}` when absent)."""
    target = config_path(workdir)
    try:
        raw = read_text_if_present(target)
    except (OSError, UnicodeDecodeError) as exc:
        raise SpeakConfigError(f"cannot read {target}: {exc}") from None
    if raw is None or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SpeakConfigError(f"invalid JSON in {target}: {exc}") from None
    if not isinstance(data, dict):
        raise SpeakConfigError(f"{target} must contain a JSON object")
    out: dict = {}
    if "engine" in data and data["engine"] is not None:
        out["engine"] = validate_engine_choice(str(data["engine"]))
    if "voice" in data and data["voice"] is not None:
        v = str(data["voice"]).strip()
        if v:
            out["voice"] = v
    if "model" in data and data["model"] is not None:
        m = str(data["model"]).strip()
        if m:
            out["model"] = m
    if "format" in data and data["format"] is not None:
        out["format"] = validate_format(str(data["format"]))
    if "speed" in data and data["speed"] is not None:
        out["speed"] = validate_speed(data["speed"])
    if "style" in data and data["style"] is not None:
        s = str(data["style"]).strip()
        if s:
            out["style"] = s
    return out


def update_config(
    workdir: Path,
    *,
    engine: str | None = None,
    voice: str | None = None,
    model: str | None = None,
    audio_format: str | None = None,
    speed: str | float | None = None,
    style: str | None = None,
    unset_keys: list[str] | tuple[str, ...] = (),
) -> dict:
    """Atomically update `<workdir>/speak.json` and return the saved dict."""
    target = config_path(workdir)
    with file_lock(_lock_path(workdir)):
        current = read_config(workdir)
        for key in unset_keys:
            norm = key.strip().lower()
            if norm not in CONFIG_KEYS:
                raise SpeakConfigError(
                    f"unknown config key {key!r} (choose from: {', '.join(CONFIG_KEYS)})"
                )
            current.pop(norm, None)
        if engine is not None:
            cleaned_eng = validate_engine_choice(engine)
            if cleaned_eng == "auto":
                current.pop("engine", None)
            else:
                current["engine"] = cleaned_eng
        if voice is not None:
            cleaned_voice = voice.strip()
            if not cleaned_voice:
                current.pop("voice", None)
            else:
                entries = [e.strip() for e in cleaned_voice.split(",") if e.strip()]
                eff_eng = current.get("engine")
                if (
                    len(entries) == 1
                    and parse_voice_entry(entries[0])[0] is None
                    and eff_eng in ENGINES
                ):
                    probe = resolve_for_engine(entries[0], eff_eng, "cli")
                    if probe.how == "custom" and probe.voice:
                        cleaned_voice = f"{eff_eng}:{probe.voice}"
                current["voice"] = cleaned_voice
        if model is not None:
            cleaned_model = model.strip()
            if cleaned_model:
                current["model"] = cleaned_model
            else:
                current.pop("model", None)
        if audio_format is not None:
            current["format"] = validate_format(audio_format)
        if speed is not None:
            current["speed"] = validate_speed(speed)
        if style is not None:
            cleaned_style = style.strip()
            if cleaned_style:
                current["style"] = cleaned_style
            else:
                current.pop("style", None)
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


def validate_pair(pattern: str, rhs: str, kind: str) -> tuple[str, str]:
    pat = (pattern or "").strip()
    val = (rhs or "").strip()
    if not pat or not val:
        raise SpeakConfigError(
            f"{kind} requires both pattern and target ('pattern => target')"
        )
    if "\n" in pat or "\r" in pat or "\n" in val or "\r" in val:
        raise SpeakConfigError(f"{kind} pattern and target must be single-line")
    if "=>" in pat:
        raise SpeakConfigError(f"{kind} pattern cannot contain '=>'")
    try:
        compiled = re.compile(pat)
    except re.error as exc:
        raise SpeakConfigError(f"invalid {kind} regex {pat!r}: {exc}") from None
    if compiled.match("") is not None:
        raise SpeakConfigError(
            f"{kind} regex {pat!r} matches empty text; narrow the pattern"
        )
    return pat, val


def parse_pair_spec(raw: str, kind: str = "replacement") -> tuple[str, str]:
    if "=>" not in (raw or ""):
        raise SpeakConfigError(
            f"invalid {kind} {raw!r} (expected 'pattern => target')"
        )
    left, right = raw.split("=>", 1)
    return validate_pair(left, right, kind)


def _read_pair_file(target: Path, kind: str) -> list[tuple[str, str]]:
    try:
        raw = read_text_if_present(target)
    except (OSError, UnicodeDecodeError) as exc:
        raise SpeakConfigError(f"cannot read {target}: {exc}") from None
    if raw is None:
        return []
    pairs: list[tuple[str, str]] = []
    for lineno, line in enumerate(raw.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            pairs.append(parse_pair_spec(stripped, kind))
        except SpeakConfigError as exc:
            raise SpeakConfigError(f"{target.name}:{lineno}: {exc}") from None
    return pairs


def _set_pair_in_file(
    workdir: Path, target: Path, pattern: str, rhs: str, kind: str
) -> str:
    pat, val = validate_pair(pattern, rhs, kind)
    with file_lock(_lock_path(workdir)):
        raw = read_text_if_present(target) or ""
        lines = raw.splitlines()
        updated_lines: list[str] = []
        replaced = False
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=>" in stripped:
                left = stripped.split("=>", 1)[0].strip()
                if left == pat:
                    if not replaced:
                        updated_lines.append(f"{pat} => {val}")
                        replaced = True
                    continue
            updated_lines.append(line)
        if not replaced:
            updated_lines.append(f"{pat} => {val}")
        atomic_write_text(target, "\n".join(updated_lines) + "\n")
    return "updated" if replaced else "added"


def _remove_pair_from_file(
    workdir: Path, target: Path, pattern: str, kind: str
) -> bool:
    needle = (pattern or "").strip()
    if not needle:
        raise SpeakConfigError(f"provide a non-empty {kind} pattern to remove")
    with file_lock(_lock_path(workdir)):
        raw = read_text_if_present(target)
        if raw is None:
            return False
        kept: list[str] = []
        removed = False
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=>" in stripped:
                left = stripped.split("=>", 1)[0].strip()
                if left == needle:
                    removed = True
                    continue
            kept.append(line)
        if removed:
            text = ("\n".join(kept) + "\n") if kept else ""
            atomic_write_text(target, text)
        return removed


def _clear_pair_file(workdir: Path, target: Path, kind: str) -> int:
    with file_lock(_lock_path(workdir)):
        current = _read_pair_file(target, kind)
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            raise SpeakConfigError(f"cannot remove {target}: {exc}") from None
        return len(current)


def read_replacements(workdir: Path) -> list[tuple[str, str]]:
    return _read_pair_file(replacements_path(workdir), "replacement")


def set_replacement(workdir: Path, pattern: str, spoken: str) -> str:
    return _set_pair_in_file(workdir, replacements_path(workdir), pattern, spoken, "replacement")


def remove_replacement(workdir: Path, pattern: str) -> bool:
    return _remove_pair_from_file(workdir, replacements_path(workdir), pattern, "replacement")


def clear_replacements(workdir: Path) -> int:
    return _clear_pair_file(workdir, replacements_path(workdir), "replacement")


def read_pronunciations(workdir: Path) -> list[tuple[str, str]]:
    return _read_pair_file(pronunciations_path(workdir), "pronunciation")


def set_pronunciation(workdir: Path, pattern: str, ipa: str) -> str:
    return _set_pair_in_file(workdir, pronunciations_path(workdir), pattern, ipa, "pronunciation")


def remove_pronunciation(workdir: Path, pattern: str) -> bool:
    return _remove_pair_from_file(workdir, pronunciations_path(workdir), pattern, "pronunciation")


def clear_pronunciations(workdir: Path) -> int:
    return _clear_pair_file(workdir, pronunciations_path(workdir), "pronunciation")


def apply_replacements(
    text: str, pairs: list[tuple[str, str]]
) -> tuple[str, list[str]]:
    applied: list[str] = []
    for pattern, spoken in pairs:
        try:
            compiled = re.compile(pattern)
            if compiled.match("") is not None:
                continue
            text, count = compiled.subn(spoken, text)
        except re.error as exc:
            print(f"tool-speak: bad replacement regex {pattern!r}: {exc}", file=sys.stderr)
            continue
        if count:
            applied.append(f"{pattern} => {spoken} (x{count})")
    return text, applied


def apply_pronunciations(
    text: str, pairs: list[tuple[str, str]]
) -> tuple[str, list[str]]:
    applied: list[str] = []
    for pattern, ipa in pairs:
        def _wrap(match: re.Match[str], target_ipa: str = ipa) -> str:
            return f"[{match.group(0)}](/{target_ipa}/)"

        try:
            compiled = re.compile(pattern)
            if compiled.match("") is not None:
                continue
            text, count = compiled.subn(_wrap, text)
        except re.error as exc:
            print(f"tool-speak: bad pronunciation regex {pattern!r}: {exc}", file=sys.stderr)
            continue
        if count:
            applied.append(f"{pattern} => /{ipa}/ (x{count})")
    return text, applied
