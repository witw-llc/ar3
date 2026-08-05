"""a8s settings — operator config at `~/.a8s/settings.json` plus knob catalog.

Writable machine-wide keys resolve:
  1. settings.json (`a8s config set`)
  2. env var when absent from settings.json
  3. bundled default

`a8s config` with no args lists every known knob — including per-agent
definition fields, registry, and network — so operators can see the full
surface even when a knob is not stored in settings.json.

`A8S_HOME` relocates the entire state dir (including settings.json).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core import BACKOFF_SCHEDULE, MAX_WAKE_ATTEMPTS, WAKE_RETRY_SCHEDULE, _a8s_dir

Group = Literal["machine", "definition", "registry", "network", "env", "constant"]


@dataclass(frozen=True)
class Knob:
    key: str
    default: Any
    group: Group
    writable: bool
    env_var: str | None = None
    note: str = ""


KNOBS: tuple[Knob, ...] = (
    # --- machine-wide (settings.json) ---
    Knob(
        "convo_max_rows",
        50_000,
        "machine",
        True,
        "A8S_CONVO_MAX_ROWS",
        "Rows retained in conversations.sqlite3 when a8s update runs housekeeping",
    ),
    Knob(
        "loop_interval",
        1.0,
        "machine",
        True,
        "A8S_LOOP_INTERVAL",
        "Default attached_loop poll seconds; a8s --interval overrides per invocation",
    ),
    Knob(
        "max_file_bytes",
        50 * 1024 * 1024,
        "machine",
        True,
        "A8S_MAX_FILE_BYTES",
        "Attachment size cap at routing time (bytes); a8s also injects TELL_FILE_MAX on wake for tell",
    ),
    Knob(
        "storage_allow_http",
        0,
        "machine",
        True,
        "A8S_STORAGE_ALLOW_HTTP",
        "Fetch attachment URLs over plaintext http as well as https (0 = https only)",
    ),
    Knob(
        "storage_receive_wait_seconds",
        900,
        "machine",
        True,
        "A8S_STORAGE_RECEIVE_WAIT_SECONDS",
        "Seconds to retry downloading remote attachment URLs before delivering ATTACHMENT_UNAVAILABLE (0 = one try)",
    ),
    Knob(
        "storage_fetch_poll_seconds",
        5,
        "machine",
        True,
        "A8S_STORAGE_FETCH_POLL_SECONDS",
        "Sleep between attachment fetch/probe attempts",
    ),
    Knob(
        "max_seen_ids",
        10000,
        "machine",
        True,
        "A8S_MAX_SEEN_IDS",
        "Cluster-wide receive dedup ring size (~/.a8s/seen-ids)",
    ),
    Knob(
        "txlog_detail_max",
        2000,
        "machine",
        True,
        "A8S_TXLOG_DETAIL_MAX",
        "Max chars stored in the transactions.sqlite3 detail column (0 = unlimited; still a preview, not full bodies)",
    ),
    Knob(
        "txlog_max_rows",
        200_000,
        "machine",
        True,
        "A8S_TXLOG_MAX_ROWS",
        "Rows retained in transactions.sqlite3 when a8s update runs housekeeping",
    ),
    # --- per-agent definition (a8s define) ---
    Knob("definition.invoke", None, "definition", False, note="Required argv template for message wakes"),
    Knob("definition.outbox_dir", ".outbox", "definition", False, note="Tell outbox under agent root (absolute OK); a8s injects TELL_OUTBOX_DIR on wake"),
    Knob("definition.files_dir", ".files", "definition", False, note="Inbound attachment root (absolute OK)"),
    Knob("definition.inbox_dir", ".inbox", "definition", False, note="File-proxy only: where wake moves inbox JSON for remote polling"),
    Knob("definition.files_ttl_hours", 48, "definition", False, note="Attachment TTL cleanup on idle (hours)"),
    Knob("definition.pause", 0, "definition", False, note="Debounce seconds before waking on a message burst"),
    Knob("definition.max_wake_seconds", None, "definition", False, note="Kill wake subprocess after N seconds (0 disables)"),
    Knob("definition.batch.invoke", None, "definition", False, note="Argv when 2+ inbox messages waiting"),
    Knob("definition.batch.limit", 5, "definition", False, note="Max messages per batch wake"),
    Knob("definition.idle.timeout", None, "definition", False, note="Seconds idle before idle.invoke (0 disables)"),
    Knob("definition.idle.invoke", None, "definition", False, note="Argv for idle/sync hooks"),
    Knob("definition.proxy", None, "definition", False, note='Set to "file" for file-proxy agents (no CLI wake)'),
    # --- registry (a8s add / a8s alias) ---
    Knob("registry.agents.<name>.root", None, "registry", False, note="Agent workspace directory"),
    Knob("registry.agents.<name>.definition", None, "registry", False, note="Path to wake JSON (optional)"),
    Knob("registry.agents.<name>.safe_dirs", None, "registry", False, note="Legacy extra attachment roots (unused for routing)"),
    Knob("registry.aliases.<name>", None, "registry", False, note="Alias member list"),
    # --- network (a8s remote / a8s storage) ---
    Knob("network.remotes.<name>", None, "network", False, note="Cross-machine MQTT transport config"),
    Knob("network.services.<name>", None, "network", False, note="Shared file storage for cross-cluster attachments"),
    # --- runtime environment ---
    Knob("A8S_HOME", None, "env", False, note="Relocate entire a8s state tree (default: ~/.config/a8s, or legacy ~/.a8s if present)"),
    Knob("TELL_OUTBOX_DIR", None, "env", False, note="Tell write path; a8s sets on wake from definition.outbox_dir"),
    # --- code constants (not in settings.json) ---
    Knob(
        "remote.backoff_schedule",
        list(BACKOFF_SCHEDULE),
        "constant",
        False,
        note="Remote publish retry delays in seconds (fixed schedule)",
    ),
    Knob(
        "wake.retry_schedule",
        list(WAKE_RETRY_SCHEDULE),
        "constant",
        False,
        note=(
            "Delays in seconds before redelivering after a failed wake; "
            f"{MAX_WAKE_ATTEMPTS} attempts then the envelope stays in trash as a dead letter"
        ),
    ),
)

DEFAULTS: dict[str, Any] = {
    k.key: k.default for k in KNOBS if k.group == "machine" and k.writable
}

ENV_VARS: dict[str, str] = {
    k.key: k.env_var for k in KNOBS if k.group == "machine" and k.env_var
}

_WRITABLE = frozenset(DEFAULTS)


__all__ = [
    "DEFAULTS",
    "ENV_VARS",
    "KNOBS",
    "Knob",
    "get_float",
    "get_int",
    "get_setting",
    "is_writable",
    "knob_by_key",
    "list_catalog",
    "list_settings",
    "load_settings_file",
    "save_settings_file",
    "settings_path",
    "set_setting",
    "unset_setting",
]


def settings_path() -> Path:
    return _a8s_dir() / "settings.json"


def is_writable(key: str) -> bool:
    return key in _WRITABLE


def knob_by_key(key: str) -> Knob | None:
    for k in KNOBS:
        if k.key == key:
            return k
    return None


def load_settings_file() -> dict[str, Any]:
    path = settings_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in _WRITABLE}


def save_settings_file(data: dict[str, Any]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = {k: data[k] for k in sorted(data) if k in _WRITABLE}
    path.write_text(json.dumps(cleaned, indent=2) + "\n", encoding="utf-8")


def _coerce(key: str, raw: str) -> Any:
    if key in (
        "convo_max_rows",
        "max_file_bytes",
        "max_seen_ids",
        "storage_allow_http",
        "storage_receive_wait_seconds",
        "storage_fetch_poll_seconds",
        "txlog_detail_max",
        "txlog_max_rows",
    ):
        return int(raw)
    if key == "loop_interval":
        return float(raw)
    return raw


def _validate(key: str, value: Any) -> Any:
    if key == "convo_max_rows":
        n = int(value)
        if n < 1:
            raise ValueError("convo_max_rows must be a positive integer")
        return n
    if key == "max_file_bytes":
        n = int(value)
        if n < 1:
            raise ValueError("max_file_bytes must be a positive integer")
        return n
    if key == "storage_allow_http":
        n = int(value)
        if n not in (0, 1):
            raise ValueError("storage_allow_http must be 0 or 1")
        return n
    if key == "storage_receive_wait_seconds":
        n = int(value)
        if n < 0:
            raise ValueError("storage_receive_wait_seconds must be zero or positive")
        return n
    if key == "storage_fetch_poll_seconds":
        n = int(value)
        if n < 1:
            raise ValueError("storage_fetch_poll_seconds must be a positive integer")
        return n
    if key == "max_seen_ids":
        n = int(value)
        if n < 1:
            raise ValueError("max_seen_ids must be a positive integer")
        return n
    if key == "txlog_detail_max":
        n = int(value)
        if n < 0:
            raise ValueError("txlog_detail_max must be zero or positive")
        return n
    if key == "txlog_max_rows":
        n = int(value)
        if n < 1:
            raise ValueError("txlog_max_rows must be a positive integer")
        return n
    if key == "loop_interval":
        f = float(value)
        if f <= 0:
            raise ValueError("loop_interval must be a positive number")
        return f
    return value


def get_setting(key: str) -> Any:
    if key not in _WRITABLE:
        knob = knob_by_key(key)
        if knob is not None:
            return knob.default
        raise KeyError(key)
    stored = load_settings_file()
    if key in stored:
        return stored[key]
    env_name = ENV_VARS.get(key)
    if env_name:
        raw = os.environ.get(env_name, "")
        if raw:
            return _coerce(key, raw)
    return DEFAULTS[key]


def get_int(key: str) -> int:
    value = get_setting(key)
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = int(DEFAULTS[key])
    return max(1, n)


def get_float(key: str) -> float:
    value = get_setting(key)
    try:
        f = float(value)
    except (TypeError, ValueError):
        f = float(DEFAULTS[key])
    return max(1e-9, f)


def set_setting(key: str, value: Any) -> None:
    if key not in _WRITABLE:
        raise KeyError(key)
    value = _validate(key, value)
    data = load_settings_file()
    data[key] = value
    save_settings_file(data)


def unset_setting(key: str) -> bool:
    if key not in _WRITABLE:
        raise KeyError(key)
    data = load_settings_file()
    if key not in data:
        return False
    del data[key]
    save_settings_file(data)
    return True


def _machine_source(key: str, stored: dict[str, Any]) -> str:
    if key in stored:
        return "settings.json"
    env_name = ENV_VARS.get(key)
    if env_name and os.environ.get(env_name, ""):
        return "env"
    return "default"


def list_settings() -> list[tuple[str, Any, Any, Any, str]]:
    """Return (key, stored, effective, default, source) for writable keys."""
    stored = load_settings_file()
    rows: list[tuple[str, Any, Any, Any, str]] = []
    for key, default in DEFAULTS.items():
        file_val = stored.get(key)
        effective = get_setting(key)
        rows.append((key, file_val, effective, default, _machine_source(key, stored)))
    return rows


_GROUP_ORDER: tuple[Group, ...] = ("machine", "definition", "registry", "network", "env", "constant")
_GROUP_LABELS = {
    "machine": "Machine-wide (a8s config set)",
    "definition": "Per-agent definition (a8s define)",
    "registry": "Registry (~/.a8s/a8s.json)",
    "network": "Network (~/.a8s/network.json)",
    "env": "Runtime environment",
    "constant": "Code constants (not in settings.json)",
}


def list_catalog() -> list[tuple[str, list[Knob]]]:
    by_group: dict[str, list[Knob]] = {g: [] for g in _GROUP_ORDER}
    for knob in KNOBS:
        by_group[knob.group].append(knob)
    return [( _GROUP_LABELS[g], by_group[g]) for g in _GROUP_ORDER if by_group[g]]
