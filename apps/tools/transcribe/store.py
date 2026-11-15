"""Working-directory hint and replacement files for tool-transcribe."""
from __future__ import annotations

import re
import sys
from pathlib import Path

from ar3.fsio import atomic_write_text, read_text_if_present
from ar3.locking import file_lock

HINTS_FILENAME = "hints.txt"
REPLACEMENTS_FILENAME = "replacements.txt"
LOCK_FILENAME = ".transcribe.lock"
LOOP_WARN_THRESHOLD = 4


class TranscribeConfigError(ValueError):
    """Raised when a hint or replacement specification is malformed."""


def hints_path(workdir: Path) -> Path:
    return Path(workdir).resolve() / HINTS_FILENAME


def replacements_path(workdir: Path) -> Path:
    return Path(workdir).resolve() / REPLACEMENTS_FILENAME


def _lock_path(workdir: Path) -> Path:
    return Path(workdir).resolve() / LOCK_FILENAME


def split_hints(values: list[str] | tuple[str, ...]) -> list[str]:
    """Split repeatable `--hint` strings on commas and newlines, stripping blanks."""
    out: list[str] = []
    for raw in values:
        for line in str(raw).splitlines():
            for part in line.split(","):
                item = part.strip()
                if item:
                    out.append(item)
    return out


def read_hints(workdir: Path) -> list[str]:
    """Read active hint terms from `<workdir>/hints.txt`."""
    try:
        raw = read_text_if_present(hints_path(workdir))
    except (OSError, UnicodeDecodeError) as exc:
        raise TranscribeConfigError(f"cannot read {hints_path(workdir)}: {exc}") from None
    if raw is None:
        return []
    hints: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            hints.append(stripped)
    return hints


def add_hints(workdir: Path, values: list[str] | tuple[str, ...]) -> tuple[int, int]:
    """Append new hints to `<workdir>/hints.txt`, preserving comments. Returns `(added, skipped)`."""
    incoming = split_hints(values)
    if not incoming:
        raise TranscribeConfigError("provide at least one non-empty hint")
    target = hints_path(workdir)
    with file_lock(_lock_path(workdir)):
        raw = read_text_if_present(target) or ""
        existing_lines = raw.splitlines()
        known = {
            ln.strip().lower()
            for ln in existing_lines
            if ln.strip() and not ln.strip().startswith("#")
        }
        added: list[str] = []
        skipped = 0
        for item in incoming:
            key = item.lower()
            if key in known:
                skipped += 1
                continue
            known.add(key)
            added.append(item)
        if added:
            updated = list(existing_lines) + added
            atomic_write_text(target, "\n".join(updated) + "\n")
    return len(added), skipped


def remove_hint(workdir: Path, term: str) -> bool:
    """Remove `term` (case-insensitive) from `<workdir>/hints.txt`."""
    needle = (term or "").strip().lower()
    if not needle:
        raise TranscribeConfigError("provide a non-empty hint to remove")
    target = hints_path(workdir)
    with file_lock(_lock_path(workdir)):
        raw = read_text_if_present(target)
        if raw is None:
            return False
        kept: list[str] = []
        removed = False
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and stripped.lower() == needle:
                removed = True
                continue
            kept.append(line)
        if removed:
            text = ("\n".join(kept) + "\n") if kept else ""
            atomic_write_text(target, text)
        return removed


def clear_hints(workdir: Path) -> int:
    """Remove all hints from `<workdir>/hints.txt`, returning the removed count."""
    target = hints_path(workdir)
    with file_lock(_lock_path(workdir)):
        current = read_hints(workdir)
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            raise TranscribeConfigError(f"cannot remove {target}: {exc}") from None
        return len(current)


def validate_replacement(pattern: str, correction: str) -> tuple[str, str]:
    pat = (pattern or "").strip()
    corr = (correction or "").strip()
    if not pat or not corr:
        raise TranscribeConfigError(
            "replacement requires both pattern and correction ('pattern => correction')"
        )
    if "\n" in pat or "\r" in pat or "\n" in corr or "\r" in corr:
        raise TranscribeConfigError("replacement pattern and correction must be single-line")
    if "=>" in pat:
        raise TranscribeConfigError("replacement pattern cannot contain '=>'")
    try:
        compiled = re.compile(pat)
    except re.error as exc:
        raise TranscribeConfigError(f"invalid replacement regex {pat!r}: {exc}") from None
    if compiled.match("") is not None:
        raise TranscribeConfigError(
            f"replacement regex {pat!r} matches empty text; narrow the pattern"
        )
    return pat, corr


def parse_replacement_spec(raw: str) -> tuple[str, str]:
    """Parse one `pattern => correction` specification."""
    if "=>" not in (raw or ""):
        raise TranscribeConfigError(
            f"invalid replacement {raw!r} (expected 'pattern => correction')"
        )
    left, right = raw.split("=>", 1)
    return validate_replacement(left, right)


def parse_cli_replacements(values: list[str] | tuple[str, ...]) -> list[tuple[str, str]]:
    return [parse_replacement_spec(v) for v in values]


def read_replacements(workdir: Path) -> list[tuple[str, str]]:
    """Read `<workdir>/replacements.txt`, validating each active rule."""
    target = replacements_path(workdir)
    try:
        raw = read_text_if_present(target)
    except (OSError, UnicodeDecodeError) as exc:
        raise TranscribeConfigError(f"cannot read {target}: {exc}") from None
    if raw is None:
        return []
    pairs: list[tuple[str, str]] = []
    for lineno, line in enumerate(raw.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            pairs.append(parse_replacement_spec(stripped))
        except TranscribeConfigError as exc:
            raise TranscribeConfigError(f"{target.name}:{lineno}: {exc}") from None
    return pairs


def set_replacement(workdir: Path, pattern: str, correction: str) -> str:
    """Create or update `pattern => correction` in `<workdir>/replacements.txt`."""
    pat, corr = validate_replacement(pattern, correction)
    target = replacements_path(workdir)
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
                        updated_lines.append(f"{pat} => {corr}")
                        replaced = True
                    continue
            updated_lines.append(line)
        if not replaced:
            updated_lines.append(f"{pat} => {corr}")
        atomic_write_text(target, "\n".join(updated_lines) + "\n")
    return "updated" if replaced else "added"


def save_replacements(workdir: Path, pairs: list[tuple[str, str]]) -> tuple[int, int]:
    """Save multiple `(pattern, correction)` pairs, updating existing patterns."""
    added = 0
    updated = 0
    for pat, corr in pairs:
        outcome = set_replacement(workdir, pat, corr)
        if outcome == "updated":
            updated += 1
        else:
            added += 1
    return added, updated


def remove_replacement(workdir: Path, pattern: str) -> bool:
    """Remove `pattern` from `<workdir>/replacements.txt`."""
    needle = (pattern or "").strip()
    if not needle:
        raise TranscribeConfigError("provide a non-empty pattern to remove")
    target = replacements_path(workdir)
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


def clear_replacements(workdir: Path) -> int:
    """Remove `<workdir>/replacements.txt`, returning the removed rule count."""
    target = replacements_path(workdir)
    with file_lock(_lock_path(workdir)):
        current = read_replacements(workdir)
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            raise TranscribeConfigError(f"cannot remove {target}: {exc}") from None
        return len(current)


def apply_replacements(
    text: str, pairs: list[tuple[str, str]]
) -> tuple[str, list[str]]:
    """Annotate regex matches with `(<match> (possible transcribe error, might be '<correction>'))`."""
    applied: list[str] = []
    for pattern, correction in pairs:
        def _annotate(match: re.Match[str], corr: str = correction) -> str:
            return f"{match.group(0)} (possible transcribe error, might be '{corr}')"

        try:
            compiled = re.compile(pattern)
            if compiled.match("") is not None:
                continue
            text, count = compiled.subn(_annotate, text)
        except re.error as exc:
            print(f"tool-transcribe: bad replacement regex {pattern!r}: {exc}", file=sys.stderr)
            continue
        if count:
            applied.append(f"{pattern} => {correction} (x{count})")
    return text, applied


def loop_run(text: str) -> int:
    """Longest run of identical short consecutive sentences (Whisper silence loops)."""
    sentences = [
        seg.strip().lower()
        for seg in re.split(r"(?<=[.!?])\s+", text or "")
        if seg.strip()
    ]
    if not sentences:
        return 0
    best = 0
    current = 0
    prev: str | None = None
    for seg in sentences:
        current = current + 1 if seg == prev and len(seg) < 60 else 0
        best = max(best, current)
        prev = seg
    return best + 1


def warn_if_loop(text: str, threshold: int = LOOP_WARN_THRESHOLD) -> int:
    run = loop_run(text)
    if run >= threshold:
        print(
            f"tool-transcribe: possible repetition loop "
            f"(longest identical short sentence run: {run})",
            file=sys.stderr,
        )
    return run
