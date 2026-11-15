"""Working-directory secret store (`secrets.json`) and reply scrubber for tool-research."""
from __future__ import annotations

import json
import os
import re
import stat
import threading
from pathlib import Path
from urllib.parse import quote, quote_plus

from ar3.locking import file_lock

STORE_FILENAME = "secrets.json"
LOCK_FILENAME = ".research.lock"
STORE_VERSION = 1
SECRET_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
MIN_VALUE_LENGTH = 4
REDACTED = "[REDACTED]"


class SecretStoreError(Exception):
    """Raised when `secrets.json` cannot be read, validated, or written."""


def store_path(workdir: Path) -> Path:
    return Path(workdir).resolve() / STORE_FILENAME


def _lock_path(workdir: Path) -> Path:
    return Path(workdir).resolve() / LOCK_FILENAME


def check_name(name: str) -> None:
    if not isinstance(name, str) or not SECRET_NAME_RE.fullmatch(name):
        raise SecretStoreError(
            "a secret name starts with A-Z and contains only A-Z, 0-9, and _ (at most 64 chars)"
        )


def check_value(value: str) -> None:
    if not isinstance(value, str) or len(value) < MIN_VALUE_LENGTH:
        raise SecretStoreError(
            f"a secret value must be at least {MIN_VALUE_LENGTH} characters"
        )
    if "\0" in value:
        raise SecretStoreError("a secret value cannot contain a NUL character")


def _parse_store(target: Path, raw: bytes) -> dict[str, str]:
    corrupt = SecretStoreError(
        f"secret store {target} is not valid; repair it or remove the file"
    )
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise corrupt from None
    if not isinstance(data, dict) or not isinstance(data.get("secrets"), dict):
        raise corrupt
    version = data.get("version")
    if type(version) is not int:
        raise corrupt
    if version > STORE_VERSION:
        raise SecretStoreError(
            f"secret store {target} is version {version}; this tool reads version {STORE_VERSION}"
        )
    if version != STORE_VERSION:
        raise corrupt
    parsed: dict[str, str] = {}
    try:
        for name, entry in data["secrets"].items():
            check_name(name)
            if not isinstance(entry, dict):
                raise corrupt
            val = entry.get("value")
            check_value(val)
            parsed[name] = val
    except SecretStoreError:
        raise corrupt from None
    return parsed


def _read_unlocked(workdir: Path) -> dict[str, str]:
    target = store_path(workdir)
    try:
        fd = os.open(target, os.O_RDONLY)
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except OSError as exc:
        why = exc.strerror or type(exc).__name__
        raise SecretStoreError(f"secret store {target} cannot be read: {why}") from None
    try:
        with os.fdopen(fd, "rb") as handle:
            if os.name == "posix":
                mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
                if mode & 0o077:
                    raise SecretStoreError(
                        f"secret store {target} is open to group or other users "
                        f"(mode {mode:04o}); fix it with: chmod 600 {target}"
                    )
            raw = handle.read()
    except OSError as exc:
        why = exc.strerror or type(exc).__name__
        raise SecretStoreError(f"secret store {target} cannot be read: {why}") from None
    return _parse_store(target, raw)


def read_secrets(workdir: Path) -> dict[str, str]:
    """Return `{NAME: value}` from `<workdir>/secrets.json` (empty dict if absent)."""
    with file_lock(_lock_path(workdir)):
        return _read_unlocked(workdir)


def _write_unlocked(workdir: Path, secrets_map: dict[str, str]) -> None:
    target = store_path(workdir)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STORE_VERSION,
        "secrets": {
            name: {"value": value}
            for name, value in sorted(secrets_map.items())
        },
    }
    body = json.dumps(payload, indent=2) + "\n"
    aside = target.parent / f".{STORE_FILENAME}.{os.getpid()}.{threading.get_ident()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(aside, flags, 0o600)
    except FileExistsError:
        aside.unlink(missing_ok=True)
        fd = os.open(aside, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(aside, target)
    except BaseException:
        aside.unlink(missing_ok=True)
        raise


def set_secret(workdir: Path, name: str, value: str) -> None:
    """Write or update `name` in `<workdir>/secrets.json` with mode `0600`."""
    check_name(name)
    check_value(value)
    with file_lock(_lock_path(workdir)):
        current = _read_unlocked(workdir)
        current[name] = value
        _write_unlocked(workdir, current)


def remove_secret(workdir: Path, name: str) -> bool:
    """Remove `name` from `<workdir>/secrets.json`. Returns True if removed."""
    check_name(name)
    with file_lock(_lock_path(workdir)):
        current = _read_unlocked(workdir)
        if name not in current:
            return False
        del current[name]
        _write_unlocked(workdir, current)
        return True


def resolve_api_key(workdir: Path, name: str = "OPENAI_API_KEY") -> tuple[str | None, list[str]]:
    """Resolve `OPENAI_API_KEY` from `<workdir>/secrets.json`, then `os.environ`.

    Returns `(api_key, sensitive_values_to_scrub)`.
    """
    stored = read_secrets(workdir)
    sensitive = [v for v in stored.values() if len(v) >= MIN_VALUE_LENGTH]
    env_val = (os.environ.get(name) or "").strip()
    if env_val and len(env_val) >= MIN_VALUE_LENGTH and env_val not in sensitive:
        sensitive.append(env_val)
    chosen = stored.get(name) or (env_val if env_val else None)
    return chosen, sensitive


def scrub_text(text: str, sensitive_values: list[str] | tuple[str, ...]) -> str:
    """Redact any stored or environment secret values (raw and encoded) from `text`."""
    if not text or not sensitive_values:
        return text
    forms: set[str] = set()
    for val in sensitive_values:
        if not val or len(val) < MIN_VALUE_LENGTH:
            continue
        raw_bytes = val.encode("utf-8", errors="replace")
        for candidate in (
            val,
            json.dumps(val)[1:-1],
            quote(raw_bytes, safe=""),
            quote_plus(raw_bytes),
        ):
            if len(candidate) >= MIN_VALUE_LENGTH:
                forms.add(candidate)
    out = text
    for needle in sorted(forms, key=len, reverse=True):
        if needle in out:
            out = out.replace(needle, REDACTED)
    return out
