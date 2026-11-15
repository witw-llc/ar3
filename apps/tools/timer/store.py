"""Persistent working-directory queue and message parser for tool-timer."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ar3.fsio import atomic_write_text, read_text_if_present
from ar3.locking import file_lock
from ar3.ulid import new as new_ulid

DEFAULT_MAX_DELAY = 604800.0
TIMERS_FILENAME = "timers.json"
META_DIRNAME = ".timers"
STORE_VERSION = 1

_DELAY_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?|\.[0-9]+)([smhdSMHD]?)$")
_UNIT_SECONDS = {
    "": 1.0,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}
_HEAD_RE = re.compile(
    r"^\s*(\S+)(?:[ \t]+([^\r\n][\s\S]*)|[ \t]*\r?\n([\s\S]*)|[ \t]*$)"
)


class TimerUsageError(ValueError):
    """Raised when a timer message or delay argument is invalid."""


class TimerStoreError(RuntimeError):
    """Raised when timers.json cannot be read or written safely."""


@dataclass(frozen=True)
class ParsedRequest:
    kind: str
    delay: float = 0.0
    body: str = ""
    target: str = ""


def format_duration(seconds: float) -> str:
    """Format non-negative seconds compactly for status and reply lines."""
    sec = max(0.0, float(seconds))
    if abs(sec - round(sec)) < 1e-6:
        whole = int(round(sec))
        return f"{whole}s"
    return f"{sec:.2f}".rstrip("0").rstrip(".") + "s"


def parse_delay(spec: str, *, max_delay: float = DEFAULT_MAX_DELAY) -> float:
    """Parse `<seconds>` or `<number><s|m|h|d>` within `0..max_delay`."""
    raw = (spec or "").strip()
    match = _DELAY_RE.fullmatch(raw)
    if match is None:
        limit = format_duration(max_delay)
        raise TimerUsageError(
            f"tool-timer: invalid delay {raw!r} (use seconds or s/m/h/d suffix, 0..{limit})"
        )
    amount = float(match.group(1))
    unit = match.group(2).lower()
    total = amount * _UNIT_SECONDS[unit]
    if not math.isfinite(total) or total < 0.0:
        limit = format_duration(max_delay)
        raise TimerUsageError(
            f"tool-timer: invalid delay {raw!r} (use seconds or s/m/h/d suffix, 0..{limit})"
        )
    if total > max_delay:
        limit = format_duration(max_delay)
        raise TimerUsageError(
            f"tool-timer: delay {format_duration(total)} exceeds MAX_DELAY ({limit})"
        )
    return total


def parse_request(
    raw_body: str,
    *,
    has_attachments: bool = False,
    max_delay: float = DEFAULT_MAX_DELAY,
) -> ParsedRequest:
    """Parse a wake body into `schedule`, `list`, or `cancel`."""
    if not raw_body or not raw_body.strip():
        raise TimerUsageError(
            "tool-timer: usage: <delay> <message> | list | cancel <id|all>"
        )
    match = _HEAD_RE.match(raw_body)
    if match is None:
        raise TimerUsageError(
            "tool-timer: usage: <delay> <message> | list | cancel <id|all>"
        )
    first = match.group(1)
    rest = match.group(2) if match.group(2) is not None else (match.group(3) or "")
    verb = first.lower()

    if verb == "list":
        if rest.strip():
            raise TimerUsageError("tool-timer: usage: list")
        if has_attachments:
            raise TimerUsageError("tool-timer: list does not take attachments")
        return ParsedRequest(kind="list")

    if verb == "cancel":
        if has_attachments:
            raise TimerUsageError("tool-timer: cancel does not take attachments")
        if "\n" in rest or "\r" in rest:
            raise TimerUsageError("tool-timer: usage: cancel <id|all>")
        parts = rest.split()
        if len(parts) != 1:
            raise TimerUsageError("tool-timer: usage: cancel <id|all>")
        return ParsedRequest(kind="cancel", target=parts[0])

    delay = parse_delay(first, max_delay=max_delay)
    if not rest.strip() and not has_attachments:
        raise TimerUsageError(
            f"tool-timer: missing callback message after delay {first!r}"
        )
    return ParsedRequest(kind="schedule", delay=delay, body=rest)


def format_timer_line(entry: dict, *, now: float | None = None, include_sender: bool = False) -> str:
    """Format one timer entry for `list` output."""
    ref = time.time() if now is None else now
    due_at = float(entry.get("due_at", ref))
    remaining = max(0.0, due_at - ref)
    due_iso = datetime.fromtimestamp(due_at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = str(entry.get("body") or "")
    first_line = body.splitlines()[0].strip() if body.strip() else ""
    if len(first_line) > 60:
        first_line = first_line[:57] + "..."
    files = entry.get("files") or []
    if files and not first_line:
        preview = f"({len(files)} file(s))"
    elif files:
        preview = f"{first_line} (+{len(files)} file(s))"
    else:
        preview = first_line or "(empty)"
    who = f"  to={entry.get('sender', '')}" if include_sender else ""
    return f"{entry['id']}  in {format_duration(remaining)}  ({due_iso}){who}  {preview}"


class TimerStore:
    """Working-directory store backing `timers.json` and `.timers/`."""

    def __init__(self, workdir: Path | str | None = None) -> None:
        self.workdir = Path(workdir or Path.cwd()).resolve()
        self.timers_file = self.workdir / TIMERS_FILENAME
        self.meta_dir = self.workdir / META_DIRNAME
        self.queue_lock_path = self.meta_dir / "queue.lock"
        self.worker_lock_path = self.meta_dir / "worker.lock"
        self.worker_info_path = self.meta_dir / "worker.json"
        self.outbox_info_path = self.meta_dir / "outbox.json"
        self.files_root = self.meta_dir / "files"

    def _read_unlocked(self) -> list[dict]:
        try:
            raw = read_text_if_present(self.timers_file)
        except (OSError, UnicodeDecodeError) as exc:
            raise TimerStoreError(f"cannot read {self.timers_file}: {exc}") from None
        if raw is None:
            return []
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise TimerStoreError(f"corrupt {self.timers_file}: {exc}") from None
        if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
            raise TimerStoreError(f"unsupported schema in {self.timers_file}")
        timers = data.get("timers")
        if not isinstance(timers, list) or not all(isinstance(item, dict) for item in timers):
            raise TimerStoreError(f"invalid timers list in {self.timers_file}")
        return list(timers)

    def _write_unlocked(self, timers: list[dict]) -> None:
        ordered = sorted(
            timers,
            key=lambda item: (float(item.get("due_at", 0.0)), str(item.get("id", ""))),
        )
        payload = json.dumps({"version": STORE_VERSION, "timers": ordered}, indent=2) + "\n"
        try:
            atomic_write_text(self.timers_file, payload)
        except OSError as exc:
            raise TimerStoreError(f"cannot write {self.timers_file}: {exc}") from None

    def record_outbox(self, outbox_dir: Path | str | None) -> Path | None:
        """Persist or resolve the outbox directory used for delayed `tell` delivery."""
        if outbox_dir:
            resolved = Path(outbox_dir).expanduser().resolve()
            try:
                atomic_write_text(
                    self.outbox_info_path,
                    json.dumps({"outbox_dir": str(resolved)}, indent=2) + "\n",
                )
            except OSError:
                pass
            return resolved
        try:
            raw = read_text_if_present(self.outbox_info_path)
            if raw is not None:
                saved = json.loads(raw).get("outbox_dir")
                if isinstance(saved, str) and saved.strip():
                    return Path(saved).expanduser().resolve()
        except (OSError, ValueError):
            pass
        candidate = self.workdir / ".outbox"
        if candidate.is_dir():
            return candidate.resolve()
        return None

    def schedule(
        self,
        sender: str,
        delay: float,
        body: str,
        attachments: list[str] | tuple[str, ...] = (),
        *,
        now: float | None = None,
    ) -> dict:
        """Stage any attachments and append a new timer entry atomically."""
        seen_names: set[str] = set()
        for src in attachments:
            base = Path(src).name
            if base in seen_names:
                raise TimerUsageError(
                    f"tool-timer: duplicate attachment filename {base!r}; rename one"
                )
            seen_names.add(base)

        timer_id = new_ulid()
        created = time.time() if now is None else float(now)
        due = created + float(delay)
        rel_files: list[str] = []
        bundle_dir = self.files_root / timer_id

        with file_lock(self.queue_lock_path):
            timers = self._read_unlocked()
            try:
                for idx, src in enumerate(attachments):
                    src_path = Path(src)
                    rel = f"{idx}/{src_path.name}"
                    dest = bundle_dir / str(idx) / src_path.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src_path, dest)
                    rel_files.append(rel)
                entry = {
                    "id": timer_id,
                    "sender": sender,
                    "body": body,
                    "files": rel_files,
                    "created_at": created,
                    "delay_seconds": float(delay),
                    "due_at": due,
                    "attempts": 0,
                    "next_attempt_at": due,
                    "claiming": None,
                }
                timers.append(entry)
                self._write_unlocked(timers)
                return entry
            except BaseException:
                shutil.rmtree(bundle_dir, ignore_errors=True)
                raise

    def list_timers(self, *, sender: str | None = None) -> list[dict]:
        """Return pending timers ordered by due_at, optionally filtered by sender."""
        with file_lock(self.queue_lock_path):
            timers = self._read_unlocked()
        if sender is not None:
            key = sender.strip().lower()
            timers = [t for t in timers if str(t.get("sender", "")).strip().lower() == key]
        return sorted(
            timers,
            key=lambda item: (float(item.get("due_at", 0.0)), str(item.get("id", ""))),
        )

    def has_pending(self) -> bool:
        with file_lock(self.queue_lock_path):
            return bool(self._read_unlocked())

    def cancel(self, target: str, *, sender: str | None = None) -> list[dict]:
        """Cancel `<id>` or `all` (scoped to `sender` when provided). Returns removed entries."""
        want_all = target.strip().lower() == "all"
        sender_key = sender.strip().lower() if sender is not None else None
        removed: list[dict] = []
        kept: list[dict] = []

        with file_lock(self.queue_lock_path):
            timers = self._read_unlocked()
            for item in timers:
                owner_ok = (
                    sender_key is None
                    or str(item.get("sender", "")).strip().lower() == sender_key
                )
                matches = owner_ok and (want_all or str(item.get("id", "")) == target.strip())
                if matches:
                    removed.append(item)
                else:
                    kept.append(item)
            if removed:
                self._write_unlocked(kept)
                for item in removed:
                    tid = str(item.get("id", ""))
                    if tid:
                        shutil.rmtree(self.files_root / tid, ignore_errors=True)
        return removed

    def attachment_paths(self, entry: dict) -> tuple[str, ...]:
        """Existing staged attachment paths for `entry`."""
        tid = str(entry.get("id", ""))
        if not tid:
            return ()
        bundle = self.files_root / tid
        found: list[str] = []
        for rel in entry.get("files") or ():
            candidate = bundle / str(rel)
            if os.path.isfile(candidate):
                found.append(str(candidate))
        return tuple(found)

    def clear_stale_claims(self) -> None:
        """Clear leftover claim stamps when a fresh worker takes worker.lock."""
        with file_lock(self.queue_lock_path):
            timers = self._read_unlocked()
            changed = False
            for item in timers:
                if item.get("claiming") is not None:
                    item["claiming"] = None
                    changed = True
            if changed:
                self._write_unlocked(timers)

    def complete_delivery(self, timer_id: str, claim: str, *, delivered: bool) -> None:
        """Remove a delivered timer, or schedule backoff retry when `tell` fails."""
        with file_lock(self.queue_lock_path):
            timers = self._read_unlocked()
            kept: list[dict] = []
            cleanup = False
            now = time.time()
            for item in timers:
                if str(item.get("id", "")) != timer_id:
                    kept.append(item)
                    continue
                if delivered:
                    cleanup = True
                    continue
                if item.get("claiming") == claim:
                    attempts = int(item.get("attempts", 0)) + 1
                    backoff = min(60.0, 2.0 ** min(attempts, 6))
                    item["attempts"] = attempts
                    item["next_attempt_at"] = now + backoff
                    item["claiming"] = None
                kept.append(item)
            self._write_unlocked(kept)
            if cleanup:
                shutil.rmtree(self.files_root / timer_id, ignore_errors=True)
