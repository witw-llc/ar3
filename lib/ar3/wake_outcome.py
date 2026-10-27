"""Opt-in local failure records for a wake that hands work to a child.

The router supplies a directory and envelope names. The child writes a failure
once; the router consumes it once. No delivery, retry, or process ownership lives
here, and a missing context leaves standalone engines unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path

from ar3.fsio import atomic_write_text
from ar3.ulid import new as new_ulid

RECIPIENT_ENV = "A8S_TURN_RECIPIENT"
ENVELOPES_ENV = "A8S_TURN_ENVELOPES"
OUTCOME_DIR_ENV = "A8S_TURN_OUTCOME_DIR"


def outcome_dir(node_dir: Path) -> Path:
    return node_dir / "wake-outcomes"


def _filenames(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(name, str) and name.endswith(".json")
        and name not in (".json", "..json")
        and "/" not in name and "\\" not in name and ":" not in name
        for name in value
    )


def origin_from_env(env: dict[str, str], agent: str | None) -> dict | None:
    recipient = env.get(RECIPIENT_ENV)
    directory = env.get(OUTCOME_DIR_ENV)
    if not agent or recipient != agent or not directory or not Path(directory).is_absolute():
        return None
    try:
        paths = json.loads(env.get(ENVELOPES_ENV, "[]"))
    except (TypeError, ValueError):
        return None
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return None
    names = list(dict.fromkeys(Path(p).name for p in paths))
    if not _filenames(names):
        return None
    return {"id": new_ulid(), "recipient": recipient,
            "directory": directory, "envelopes": names}


def _valid_id(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 26
            and all(c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for c in value))


def valid_origin(origin: object, recipient: str | None = None) -> bool:
    return (
        isinstance(origin, dict)
        and _valid_id(origin.get("id"))
        and isinstance(origin.get("recipient"), str) and bool(origin["recipient"])
        and (recipient is None or origin["recipient"] == recipient)
        and isinstance(origin.get("directory"), str)
        and Path(origin["directory"]).is_absolute()
        and _filenames(origin.get("envelopes"))
    )


def report_failure(origin: dict | None, reason: str, *, session: bool = False) -> bool:
    if not valid_origin(origin):
        return False
    record = {"id": origin["id"], "recipient": origin["recipient"],
              "envelopes": [] if session else origin["envelopes"],
              "reason": reason[:4096]}
    suffix = "-session" if session else ""
    try:
        atomic_write_text(Path(origin["directory"]) / f"{origin['id']}{suffix}.json",
                          json.dumps(record), mode=0o600)
    except OSError:
        return False
    return True


def take_failures(directory: Path, recipient: str):
    """Discard before handing each valid record to its one-try consumer."""
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = None
        try:
            path.unlink()
        except OSError:
            continue
        if (
            isinstance(record, dict) and record.get("recipient") == recipient
            and _valid_id(record.get("id"))
            and isinstance(record.get("reason"), str)
            and _filenames(record.get("envelopes"))
        ):
            yield record
