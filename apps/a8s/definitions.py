"""a8s definitions — single `invoke` verb and argv interpolation.

Each agent has a definition JSON (built-in or custom) that encodes one argv
under the `invoke` key. `build_command` substitutes `$SENDER` / `$RECIPIENT`
/ `$MESSAGE` / `$TIMESTAMP` / `$AGE` / `$META` / `$A8S_DIR` /
`$DEFINITION_PATH` into it, plus any per-node a8s vars
(`a8s vars <name> set KEY value`) as `$KEY`.
`$DEFINITION_PATH` is the resolved path of the agent's own definition file, so
a self-contained node (e.g. r4t) can read its own definition for settings the
wire does not carry.

`$META` is the envelope's `meta` object as verbatim JSON — protocol metadata
one node stamps for another (r4t's message class). a8s carries it and
hands it to the wake; it never reads inside, so the vocabulary belongs to the
nodes at the edges and a8s learns nothing about any node's protocol.

A8s vars are NOT process environment variables — they live on the agent in
the registry and expand only through this interpolator. A `$NAME` that is
neither a built-in placeholder nor a set a8s var is a hard error. Process
environment for a wake is a separate knob, `definition.env`, and the two never
meet: a var reaches argv, an `env` entry reaches the child's environment.

Vars also reach the three mailbox path fields — `outbox_dir`, `inbox_dir`,
`files_dir` — through `_expand_path_field`, which adds one built-in of its own,
`$NODE` (the registered node name), and refuses the per-message built-ins. That
is what lets two nodes rooted at one repo share a definition and still own
separate mailboxes. The line the asymmetry follows: a mailbox path is a value
a8s itself computes and consumes, so a8s may interpolate into it; `env` is a
value a8s hands to a stranger unread, so a8s must not.

Strict opacity: the recipient sees only sender + message
content — no `alias` or `others_count` leak. A direct tell and an
alias-fanned tell produce the same prompt shape, distinguished only by what
`$RECIPIENT` resolves to (the original `to` field, which is the alias name
for fanned messages and the agent name for direct ones — same as a public
mailing list: you know it came via the list, you don't know who else got it).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from core import (
    DEFINITIONS_DIR,
    MARKER_FILES,
    SCRIPT_DIR,
    resolve_files_path,
    resolve_inbox_path,
    resolve_outbox_path,
    user_definitions_dir,
)
from registry import load_registry, resolve_recipient
from settings import get_setting

from ark import clock

ATTACHED_FILE_PREFIX = "ATTACHED FILE: "
ATTACHMENT_FAILURE_PREFIX = "ATTACHMENT UNAVAILABLE: "

BUILTIN_PLACEHOLDERS = frozenset({
    "SENDER",
    "RECIPIENT",
    "MESSAGE",
    "TIMESTAMP",
    "AGE",
    "META",
    "NOW",
    "A8S_DIR",
    "DEFINITION_PATH",
})

# Built-ins for the three mailbox path fields. `$NODE` is per-node and always
# defined, which is what makes it collision-free between two registrations on
# one root; the argv built-ins above are per-message and have no value at the
# time a path is resolved. Kept out of BUILTIN_PLACEHOLDERS so `_expand_argv`
# still refuses `$NODE` in argv rather than substituting a value it does not
# have — the name is reserved from `a8s vars` either way, so promoting it to
# argv later stays compatible.
PATH_FIELD_PLACEHOLDERS = frozenset({"NODE"})

RESERVED_PLACEHOLDERS = BUILTIN_PLACEHOLDERS | PATH_FIELD_PLACEHOLDERS

MAILBOX_PATH_FIELDS = ("outbox_dir", "inbox_dir", "files_dir")

PLACEHOLDER_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class UndefinedVarsError(ValueError):
    """Definition argv referenced `$NAME` that is not a built-in and not set."""

    def __init__(self, names: list[str]):
        self.names = list(names)
        label = "undefined a8s var" if len(self.names) == 1 else "undefined a8s vars"
        refs = ", ".join(f"${n}" for n in self.names)
        super().__init__(f"{label}: {refs}")


def validate_var_name(name: str) -> str:
    """Return the canonical (uppercase) a8s var key, or raise ValueError.

    Names are case-insensitive: ``model`` and ``MODEL`` are the same var.
    """
    if not VAR_NAME_RE.match(name):
        raise ValueError(
            f"var name must match [A-Za-z_][A-Za-z0-9_]*: {name!r}"
        )
    canon = name.upper()
    if canon in RESERVED_PLACEHOLDERS:
        raise ValueError(f"var name {name!r} is reserved for built-in interpolation")
    return canon


def load_agent_vars(name: str) -> dict[str, str]:
    """Per-node a8s vars from the registry (`agents.<name>.vars`).

    Keys are returned uppercase (canonical). Duplicate spellings collapse.
    """
    match = resolve_recipient(name)
    if match is None:
        return {}
    _, info = match
    raw = info.get("vars")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str):
            out[k.upper()] = v
    return out


def placeholder_names(argv: list[str]) -> set[str]:
    """All `$NAME` identifiers referenced in an argv template."""
    found: set[str] = set()
    for a in argv:
        for m in PLACEHOLDER_RE.finditer(a):
            found.add(m.group(1))
    return found


def default_definition_path(kind: str) -> Path:
    return DEFINITIONS_DIR / f"{kind}.json"


def definition_stem(raw: str) -> str:
    """Basename without ``.json`` — the short name used for bare resolution."""
    name = Path(raw).name
    if name.endswith(".json"):
        name = name[:-5]
    return name


def builtin_definition_stems() -> set[str]:
    return {p.stem for p in DEFINITIONS_DIR.glob("*.json") if p.is_file()}


def list_definition_entries() -> list[tuple[str, str, Path]]:
    """All known templates as ``(name, source, path)``, sorted by name.

    ``source`` is ``builtin`` (repo) or ``user`` (``~/.config/a8s/definitions``).
    Builtin wins when both exist for the same stem.
    """
    entries: dict[str, tuple[str, str, Path]] = {}
    user_dir = user_definitions_dir()
    if user_dir.is_dir():
        for p in sorted(user_dir.glob("*.json")):
            if p.is_file():
                entries[p.stem] = (p.stem, "user", p.resolve())
    for p in sorted(DEFINITIONS_DIR.glob("*.json")):
        if p.is_file():
            entries[p.stem] = (p.stem, "builtin", p.resolve())
    return [entries[k] for k in sorted(entries)]


def resolve_definition_arg(spec: str) -> Path:
    """Resolve an `a8s add` / `a8s define` definition argument.

    A bare name — no path separator, no `.json` suffix (`filedrop`, `r4t`) —
    is a definition NAME and resolves only against bundled `definitions/`,
    then user-installed ``~/.config/a8s/definitions/``. The working directory is
    deliberately not consulted for it, so an unrelated same-named file next to
    the caller (the repo-root `r4t` shim) cannot shadow the definition.
    Anything else is a filesystem path first, with a bare `<name>.json`
    falling back to the same two definition dirs.
    """
    raw = Path(spec).expanduser()
    is_name = raw.name == spec and not spec.endswith(".json")
    if not is_name:
        if raw.is_file():
            return raw.resolve()
        try:
            resolved = raw.resolve()
            if resolved.is_file():
                return resolved
        except OSError:
            pass

    if len(raw.parts) == 1:
        name = raw.name
        for base in (DEFINITIONS_DIR, user_definitions_dir()):
            candidates = [base / name]
            if not name.endswith(".json"):
                candidates.append(base / f"{name}.json")
            for cand in candidates:
                if cand.is_file():
                    return cand.resolve()

    raise FileNotFoundError(spec)


def is_file_proxy(definition: dict) -> bool:
    return definition.get("proxy") == "file"


def files_ttl_seconds(definition: dict) -> float:
    hours = definition.get("files_ttl_hours", 48)
    try:
        h = float(hours)
    except (TypeError, ValueError):
        h = 48.0
    return max(0.0, h * 3600)


def _expand_path_field(
    spec: object, field: str, node: str, vars: dict[str, str] | None
) -> str | None:
    """Interpolate a mailbox path field. Node vars plus `$NODE`; nothing else.

    Returns None for an absent field, so the caller's `.outbox` / `.inbox` /
    `.files` default applies. Raises ValueError for a non-string or empty
    field, and UndefinedVarsError for a `$NAME` that is neither `$NODE` nor a
    set var.

    There is no partial expansion and no fallback to the default: a path that
    half-resolves is a plausible directory that is silently the wrong one, and
    two nodes on one root whose paths both collapse to the default share a
    mailbox again — the collision this interpolation exists to remove.
    """
    if spec is None:
        return None
    if not isinstance(spec, str):
        raise ValueError(f"definition {field} must be a string")
    if not spec.strip():
        raise ValueError(f"definition {field} must not be empty")
    values = {k.upper(): v for k, v in (vars or {}).items()}
    values["NODE"] = node
    refs = {n.upper() for n in placeholder_names([spec])}
    missing = sorted(n for n in refs if n not in values)
    if missing:
        raise UndefinedVarsError(missing)
    out = PLACEHOLDER_RE.sub(lambda m: values[m.group(1).upper()], spec)
    if not out.strip():
        raise ValueError(f"definition {field} expanded to empty")
    return out


def resolve_outbox_dir(
    agent_root: Path,
    definition: dict,
    node: str = "",
    vars: dict[str, str] | None = None,
) -> Path:
    """Outbox path from definition `outbox_dir` (default `.outbox` under root)."""
    spec = _expand_path_field(definition.get("outbox_dir"), "outbox_dir", node, vars)
    return resolve_outbox_path(agent_root, spec)


def resolve_outbox_dir_for_agent(name: str, agent_root: Path) -> Path:
    try:
        definition = load_definition(name)
    except (FileNotFoundError, RuntimeError):
        definition = {}
    return resolve_outbox_dir(agent_root, definition, name, load_agent_vars(name))


def resolve_files_dir(
    agent_root: Path,
    definition: dict,
    node: str = "",
    vars: dict[str, str] | None = None,
) -> Path:
    """Incoming attachment root from definition `files_dir` (default `.files`)."""
    spec = _expand_path_field(definition.get("files_dir"), "files_dir", node, vars)
    return resolve_files_path(agent_root, spec)


def resolve_files_dir_for_agent(name: str, agent_root: Path) -> Path:
    try:
        definition = load_definition(name)
    except (FileNotFoundError, RuntimeError):
        definition = {}
    return resolve_files_dir(agent_root, definition, name, load_agent_vars(name))


def resolve_inbox_dir(
    agent_root: Path,
    definition: dict,
    node: str = "",
    vars: dict[str, str] | None = None,
) -> Path:
    """File-proxy delivery dir from definition `inbox_dir` (default `.inbox`)."""
    spec = _expand_path_field(definition.get("inbox_dir"), "inbox_dir", node, vars)
    return resolve_inbox_path(agent_root, spec)


def resolve_inbox_dir_for_agent(name: str, agent_root: Path) -> Path:
    try:
        definition = load_definition(name)
    except (FileNotFoundError, RuntimeError):
        definition = {}
    return resolve_inbox_dir(agent_root, definition, name, load_agent_vars(name))


def resolve_definition_path(name: str) -> str:
    """The resolved path of `name`'s definition file (custom if the registry
    names one, else the bundled default) — the value `$DEFINITION_PATH` expands
    to. Mirrors `load_definition`'s resolution without reading the file."""
    reg = load_registry()
    info = reg.get(name) or {}
    return str(Path(info.get("definition") or default_definition_path("default")).expanduser())


def load_definition(name: str) -> dict:
    """Load the JSON definition for `name`. Every agent always has one — if
    the registry lacks an explicit `definition` field, falls back to the
    bundled `apps/a8s/definitions/default.json` (a dummy CLI that prints
    'not configured' and the received prompt).

    Definitions encode argv with `$SENDER`, `$RECIPIENT`, `$MESSAGE`, and
    `$A8S_DIR` placeholders. See apps/a8s/definitions/*.json.
    """
    reg = load_registry()
    info = reg.get(name) or {}
    custom = info.get("definition") or str(default_definition_path("default"))
    path = Path(custom).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"definition file missing: {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.loads(f.read())
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"definition load failed for {path}: {e}") from e


def _file_lines(msg: dict, files_root: Path) -> list[str]:
    files = msg.get("files") or []
    if not files:
        return []
    msg_id = (msg.get("id") or "").strip()
    if not msg_id:
        return []
    out = [""]
    for entry in files:
        if (entry.get("path") or "").strip():
            continue
        filename = (entry.get("filename") or "").strip()
        if not filename:
            continue
        if entry.get("error"):
            detail = (entry.get("detail") or entry.get("error") or "").strip()
            out.append(f"{ATTACHMENT_FAILURE_PREFIX}{filename}: {detail}")
            continue
        path = (files_root / msg_id / filename).resolve()
        out.append(f"{ATTACHED_FILE_PREFIX}{path}")
    return out


def _message_body(msg: dict, files_root: Path) -> str:
    """Compose the `$MESSAGE` body: content plus any ATTACHED FILE: lines."""
    content = msg.get("content", "")
    lines = _file_lines(msg, files_root)
    if not lines:
        return content
    return "\n".join([content, *lines])


def _parse_iso(date_str: str) -> datetime | None:
    """Parse the ISO timestamp we write into messages (`...Z` UTC). Returns
    None for empty / unparseable input."""
    if not date_str:
        return None
    try:
        if date_str.endswith("Z"):
            return datetime.fromisoformat(date_str[:-1] + "+00:00")
        return datetime.fromisoformat(date_str)
    except ValueError:
        return None


def _format_age(date_str: str, *, now: datetime | None = None) -> str:
    """Convert an ISO timestamp into a human-readable 'N units ago' string.
    Empty for missing/unparseable input. `now` is injectable for tests."""
    ts = _parse_iso(date_str)
    if ts is None:
        return ""
    if now is None:
        now = datetime.now(timezone.utc)
    seconds = max(0, int((now - ts).total_seconds()))
    if seconds < 60:
        n, unit = seconds, "second"
    elif seconds < 3600:
        n, unit = seconds // 60, "minute"
    elif seconds < 86400:
        n, unit = seconds // 3600, "hour"
    elif seconds < 7 * 86400:
        n, unit = seconds // 86400, "day"
    else:
        n, unit = seconds // (7 * 86400), "week"
    plural = "" if n == 1 else "s"
    return f"{n} {unit}{plural} ago"


def envelope_meta(msg: dict) -> str:
    """The envelope's `meta` object as compact JSON — the `$META` value.

    Opaque protocol metadata between nodes: a8s copies it along the wire and
    hands it to the wake verbatim, never reading a key. A missing or non-object
    `meta` expands empty, exactly as `$SENDER` does on a senderless wake."""
    meta = msg.get("meta")
    if not isinstance(meta, dict) or not meta:
        return ""
    return json.dumps(meta, sort_keys=True, separators=(",", ":"))


def _expand_argv(
    argv: list[str],
    sender: str,
    recipient: str,
    message: str,
    timestamp: str = "",
    age: str = "",
    definition_path: str = "",
    vars: dict[str, str] | None = None,
    meta: str = "",
) -> list[str]:
    """Expand placeholders in argv.

    Built-ins:
      - `$SENDER`     sender's canonical name (empty for senderless prompts)
      - `$RECIPIENT`  what the sender wrote in `to` (alias for fanned, agent for direct)
      - `$MESSAGE`    content + any ATTACHED FILE: lines
      - `$TIMESTAMP`  ISO 8601 UTC time the message was queued
      - `$AGE`        human-readable age relative to now
      - `$META`       the envelope's `meta` object as JSON (empty when absent)
      - `$NOW`        wake time in this machine's zone, e.g. `2026-08-16 13:22 PDT`
      - `$A8S_DIR`    the apps/a8s/ directory
      - `$DEFINITION_PATH`  this agent's definition file path

    Plus per-node a8s vars (`vars`) as `$KEY` (case-insensitive). Not OS
    environment. Any `$NAME` that is neither a built-in nor present in `vars`
    raises ``UndefinedVarsError``.
    """
    node_vars = {k.upper(): v for k, v in (vars or {}).items()}
    refs = {n.upper() for n in placeholder_names(argv)}
    missing = sorted(
        n for n in refs if n not in BUILTIN_PLACEHOLDERS and n not in node_vars
    )
    if missing:
        raise UndefinedVarsError(missing)

    values: dict[str, str] = {
        **node_vars,
        "SENDER": sender,
        "RECIPIENT": recipient,
        "MESSAGE": message,
        "TIMESTAMP": timestamp,
        "AGE": age,
        "META": meta,
        # `$TIMESTAMP` stays the message's stored UTC — definitions pick it
        # deliberately because it is machine-readable and stable. `$NOW` is the
        # wake's own local reading, which is what makes *tomorrow* resolvable
        # for the model beside `$AGE`'s relative one.
        "NOW": clock.stamp(),
        "A8S_DIR": str(SCRIPT_DIR),
        "DEFINITION_PATH": definition_path,
    }

    def repl(m: re.Match[str]) -> str:
        return values[m.group(1).upper()]

    return [PLACEHOLDER_RE.sub(repl, a) for a in argv]


def build_command(
    definition: dict,
    msg: dict,
    files_root: Path,
    definition_path: str = "",
    vars: dict[str, str] | None = None,
) -> list[str]:
    """Pick the `invoke` argv from `definition` and expand interpolation
    variables. There is one verb — every routed message is a `tell` — so
    no dispatch table is needed.

    `files_root` is the node's already-resolved attachment root
    (`Participant.files_path()`). Resolving it here instead would mean
    re-interpolating `files_dir` from a node name and vars this function does
    not carry; the registry resolves every mailbox path exactly once.

    `$TIMESTAMP` and `$AGE` come from `msg["date"]`; both fall back to
    empty for messages that somehow lack a date field (defensive — every
    `_write_outbox` stamps one)."""
    argv = definition.get("invoke")
    if not argv:
        raise ValueError("definition missing 'invoke'")
    sender = (msg.get("from") or "").strip()
    recipient = (msg.get("to") or "").strip()
    body = _message_body(msg, files_root)
    date_str = (msg.get("date") or "").strip()
    age = _format_age(date_str)
    return _expand_argv(
        list(argv),
        sender,
        recipient,
        body,
        date_str,
        age,
        definition_path,
        vars=vars,
        meta=envelope_meta(msg),
    )


def build_idle_command(
    definition: dict,
    agent_name: str,
    definition_path: str = "",
    vars: dict[str, str] | None = None,
) -> list[str] | None:
    """Pick the `idle.invoke` argv from `definition` and expand the same
    interpolation variables `build_command` does. Returns None if the agent
    has no idle config, or if its `invoke` argv is missing/empty.

    Idle invocations have no incoming message, so $SENDER, $MESSAGE,
    $TIMESTAMP, and $AGE expand to empty strings. $RECIPIENT is set to the
    agent's own name so a definition like `["claude", "--continue", "-p",
    "$RECIPIENT idle wake"]` reads naturally."""
    idle = definition.get("idle")
    if not isinstance(idle, dict):
        return None
    argv = idle.get("invoke")
    if not argv:
        return None
    return _expand_argv(
        list(argv), "", agent_name, "", "", "", definition_path, vars=vars
    )


DEFAULT_BATCH_PAUSE_SECONDS = 3.0


def pause_seconds(definition: dict) -> float:
    """Seconds of inbox quiet before a wake fires. An agent that declares
    `batch.invoke` debounces by default, because collecting a burst is the
    whole reason it asked to be woken in batches; everything else stays
    immediate. An explicit `0` is honored as off — only an absent or
    unreadable value falls back to the default."""
    default = (
        DEFAULT_BATCH_PAUSE_SECONDS if has_batch_invoke(definition) else 0.0
    )
    raw = definition.get("pause")
    if raw is None:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else 0.0


def max_wake_seconds(definition: dict) -> float | None:
    """Returns `definition.max_wake_seconds` as a positive float, or None if
    not configured / not a positive number."""
    raw = definition.get("max_wake_seconds")
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def has_batch_invoke(definition: dict) -> bool:
    batch = definition.get("batch")
    if not isinstance(batch, dict):
        return False
    return bool(batch.get("invoke"))


def batch_limit(definition: dict) -> int:
    batch = definition.get("batch")
    if not isinstance(batch, dict):
        return 5
    raw = batch.get("limit", 5)
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 5
    return max(1, v)


def batch_format(definition: dict) -> str:
    """`batch.format`: `"envelopes"` only when that exact word is set
    (case-insensitive, stripped); anything else, including absent or
    garbage, is `"prompt"`. Unknown words must never silently acquire
    meaning."""
    batch = definition.get("batch")
    if not isinstance(batch, dict):
        return "prompt"
    raw = batch.get("format")
    if not isinstance(raw, str):
        return "prompt"
    return "envelopes" if raw.strip().lower() == "envelopes" else "prompt"


class BatchEntry(NamedTuple):
    """One inbox envelope handed to `build_batch_command`. `msg` is the
    parsed envelope dict; it is None if the file failed to parse, in which
    case `name`/`error` are used to render a visible placeholder instead of
    silently dropping the message (a batch wake must account for every file
    it trashed)."""
    msg: dict | None
    name: str
    error: str | None = None


def format_batch_message(msg: dict) -> str:
    """Render one envelope as a '----' block, in the same voice
    `build_command` uses for a single message: sender, human age (falling
    back to the raw ISO date, then 'unknown time'), and content."""
    sender = (msg.get("from") or "").strip() or "unknown"
    date_str = (msg.get("date") or "").strip()
    age = _format_age(date_str) or date_str or "unknown time"
    content = msg.get("content", "")
    return f"----\n{sender} sent ({age}): {content}"


def format_batch_placeholder(name: str, error: str) -> str:
    """Visible stand-in for an envelope file that failed to parse — batch
    delivery must never silently drop a message."""
    return f"---- [unreadable message file {name}: {error}]"


def build_batch_prompt(recipient: str, entries: list[BatchEntry]) -> str:
    """Compose the single prompt string passed to `batch.invoke` when
    `batch.format` is `"prompt"` (the default): the same header
    `build_command` implies via the single-message CLI convention,
    followed by one '----' block per entry (or a placeholder for one that
    failed to parse).

    The header opens with the local time because this string is read by a
    model, and a model handed nothing but UTC concludes it lives there — after
    which every *today* and *tomorrow* it writes lands a day off.

    This replaces the old contract of handing the invoked command N raw
    envelope file paths and trusting it to re-parse them — that second,
    schema-divergent parser (in the external `bulk-invoke` helper) is what
    silently broke batch delivery. Composing the prompt here means there is
    only one place in the pipeline that understands the envelope schema."""
    header = (
        f"Local time is {clock.stamp()}. Every date and time you read or write "
        "is this zone unless it carries an explicit offset.\n"
        f"You are receiving messages as '{recipient}'. Send with the bash CLI "
        "`tell`, body on stdin with the delimiter quoted so nothing expands "
        "($ ` \\ arrive byte-exact):\n"
        "    tell <recipient> - <<'EOF'\n"
        "    <your message>\n"
        "    EOF\n"
        "Attach files with `tell --attach /path/to/file <recipient> -`. "
        "Delivery is asynchronous; do not wait for a reply."
    )
    blocks = [header]
    for entry in entries:
        if entry.msg is None:
            blocks.append(
                format_batch_placeholder(entry.name, entry.error or "unknown error")
            )
        else:
            blocks.append(format_batch_message(entry.msg))
    return "\n".join(blocks)


def build_batch_envelopes(entries: list[BatchEntry]) -> str:
    """Compact JSON array of parsed envelopes for `batch.format: envelopes`.
    An unreadable entry becomes `{"_unreadable": <name>, "error": <error>}`
    so the wake still accounts for every file it trashed."""
    out: list[dict] = []
    for entry in entries:
        if entry.msg is None:
            out.append({
                "_unreadable": entry.name,
                "error": entry.error or "unknown error",
            })
        else:
            out.append(entry.msg)
    return json.dumps(out, sort_keys=True, separators=(",", ":"))


def build_batch_command(
    definition: dict,
    agent_name: str,
    entries: list[BatchEntry],
    definition_path: str = "",
    vars: dict[str, str] | None = None,
) -> list[str]:
    """Expand `batch.invoke` like idle (no incoming message) and append ONE
    trailing argv element: a composed prose prompt (`batch.format` absent or
    `"prompt"`) or a JSON array of envelopes (`"envelopes"`)."""
    batch = definition.get("batch")
    if not isinstance(batch, dict):
        raise ValueError("definition missing 'batch'")
    argv = batch.get("invoke")
    if not argv:
        raise ValueError("definition missing 'batch.invoke'")
    cmd = _expand_argv(
        list(argv), "", agent_name, "", "", "", definition_path, vars=vars
    )
    if batch_format(definition) == "envelopes":
        cmd.append(build_batch_envelopes(entries))
    else:
        cmd.append(build_batch_prompt(agent_name, entries))
    return cmd


def idle_timeout_seconds(definition: dict) -> float | None:
    """Returns `definition.idle.timeout` as a positive float, or None if
    not configured / not a positive number. Loose typing tolerated:
    `"60"` strings parse the same as `60`."""
    idle = definition.get("idle")
    if not isinstance(idle, dict):
        return None
    raw = idle.get("timeout")
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


WAKE_SHELL_LOGIN = "login"
IS_WINDOWS = os.name == "nt"


def definition_env(definition: dict) -> dict[str, str]:
    """`definition.env` — process environment declared for every wake.

    Literal strings only: no `$NAME` expansion, because these are OS variables
    handed to the child, not argv the interpolator owns. `a8s vars` is the
    other knob and stays argv-only.
    """
    raw = definition.get("env")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("definition env must be an object of NAME: value strings")
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not k or "=" in k or "\0" in k:
            raise ValueError(f"definition env name is not usable as a variable: {k!r}")
        if not isinstance(v, str):
            raise ValueError(f"definition env {k}: value must be a string")
        out[k] = v
    return out


def wake_env(definition: dict) -> dict[str, str]:
    """The environment a node declares, layered over the handler's own.

    `definition.env` wins over what `a8s start`'s shell happened to carry; the
    machine-wide `wake_path` fills in PATH for every node that does not name
    one. An unset `wake_path` means inherit, which is the handler's PATH.

    Routing variables are NOT in here. a8s injects those on top of this layer
    so a definition cannot point its own outbox somewhere else.
    """
    env = definition_env(definition)
    if "PATH" in env:
        return env
    fallback = str(get_setting("wake_path") or "").strip()
    if not fallback:
        return env
    return {"PATH": fallback, **env}


def wake_shell(definition: dict) -> str | None:
    """`definition.wake_shell` — `"login"` or nothing.

    One value, because it names one mechanism. Anything else is a typo the
    operator needs to see rather than a word that quietly means nothing.
    """
    raw = definition.get("wake_shell")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError('definition wake_shell must be the string "login"')
    value = raw.strip().lower()
    if not value:
        return None
    if value != WAKE_SHELL_LOGIN:
        raise ValueError(f'definition wake_shell must be "login", not {raw!r}')
    return WAKE_SHELL_LOGIN


def login_shell() -> str:
    """The operator's own shell. Which startup files a wrap reads depends on
    the shell, so hardcoding one would source the wrong rc on half the boxes
    the suite runs on."""
    return os.environ.get("SHELL", "").strip() or "/bin/sh"


def wrap_wake_argv(definition: dict, argv: list[str]) -> list[str]:
    """The argv a8s actually spawns, wrapped when the node opted in.

    `-ilc` in that order: `-c` last, because a shell reads the first
    non-option word after `-c` as the command and everything following it as
    positional parameters — `-c -l "cmd"` runs `-l` and makes `cmd` `$0`.
    """
    if wake_shell(definition) is None:
        return list(argv)
    if IS_WINDOWS:
        raise ValueError(
            'definition wake_shell "login" is POSIX-only; Windows has no login '
            "shell to wrap — declare definition.env.PATH or wake_path instead"
        )
    return [login_shell(), "-ilc", shlex.join(argv)]


# Programs that run another program. A definition that wraps its harness in
# one of these resolves argv[0] fine and fails *inside* the wrapper, so the
# `FileNotFoundError` guard around the spawn never sees it and the operator
# gets the wrapper's diagnostic instead of a8s's:
#   flock: failed to execute claude: No such file or directory
#
# Each entry is (flags that take a separate value, fixed operands before the
# command). Both are needed: `nice -n 10 claude` and `timeout 60 claude` skip
# different things for different reasons.
_WRAPPERS: dict[str, tuple[frozenset[str], int]] = {
    "env":     (frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}), 0),
    "nohup":   (frozenset(), 0),
    "setsid":  (frozenset(), 0),
    "time":    (frozenset({"-f", "--format", "-o", "--output"}), 0),
    "nice":    (frozenset({"-n", "--adjustment"}), 0),
    "ionice":  (frozenset({"-c", "-n", "--class", "--classdata"}), 0),
    "stdbuf":  (frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}), 0),
    "sudo":    (frozenset({"-u", "--user", "-g", "--group"}), 0),
    "doas":    (frozenset({"-u", "-C"}), 0),
    "timeout": (frozenset({"-s", "--signal", "-k", "--kill-after"}), 1),
    "flock":   (frozenset({"-w", "--wait", "--timeout", "-E", "--conflict-exit-code"}), 1),
}

_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})

_MAX_WRAPPER_DEPTH = 4


def harness_program(argv: list[str]) -> str | None:
    """The program a definition actually depends on being installed.

    `argv[0]` is not it whenever the definition wraps the harness, which the
    real-world ones do for locking and timeouts. Returns None when the argv
    cannot be reduced to one program name — notably `sh -c`, where the command
    lives inside a shell string and guessing at it would be worse than saying
    nothing.
    """
    i = 0
    for _ in range(_MAX_WRAPPER_DEPTH):
        if i >= len(argv):
            return None
        tok = argv[i]
        base = Path(tok).name
        if base in _SHELLS:
            return None
        entry = _WRAPPERS.get(base)
        if entry is None:
            return tok
        value_flags, operands = entry
        i += 1
        while i < len(argv) and argv[i].startswith("-"):
            takes_value = argv[i] in value_flags and "=" not in argv[i]
            i += 1
            if takes_value and i < len(argv):
                i += 1
        if base == "env":
            while i < len(argv) and "=" in argv[i] and not argv[i].startswith(os.sep):
                i += 1
        i += operands
    return None


def harness_is_resolvable(
    program: str, env: dict[str, str] | None = None, cwd: Path | None = None
) -> bool:
    """Whether `program` can be executed from `env`'s PATH.

    A path with a separator in it never goes through PATH: a wake runs with
    its CWD set to the node's root, so a relative one (`./curtis`) is checked
    against `cwd` — the caller passes the node's root, not the shell's own
    working directory, which is irrelevant to what the wake will see. A bare
    name goes through PATH. `env` defaults to this process's environment,
    which is exactly what `a8s start` hands the node — that equivalence is
    the whole point of probing here rather than at first wake.
    """
    if not program:
        return False
    if os.sep in program or (os.altsep and os.altsep in program):
        p = Path(program)
        if not p.is_absolute() and cwd is not None:
            p = Path(cwd) / p
        return os.access(p, os.X_OK)
    path = (env or os.environ).get("PATH")
    return shutil.which(program, path=path) is not None


def _autodiscover_definition(root: Path) -> tuple[str, str]:
    """Look for marker files (CLAUDE.md/GEMINI.md/CODEX.md) directly in `root`
    and pick the matching built-in definition. Always returns a usable path:
    falls back to `default.json` (the dummy fallback) if no single marker
    matches. Returns (definition_path, note)."""
    found: list[tuple[str, str]] = []
    for marker_name, kind in MARKER_FILES.items():
        if (root / marker_name).is_file():
            found.append((marker_name, kind))
    marker_names = [m for m, _ in found]
    default_fallback = str(default_definition_path("default"))
    if len(found) == 1:
        kind = found[0][1]
        path = default_definition_path(kind)
        if path.is_file():
            return str(path), f"auto-detected via {marker_names[0]}"
        return default_fallback, f"marker {marker_names[0]} found but {path} missing — using default fallback"
    if len(found) > 1:
        return default_fallback, f"multiple markers ({', '.join(marker_names)}) — using default fallback; re-add with explicit definition to pick one"
    return default_fallback, "no marker file — using default fallback (run `a8s define` to wire a real CLI)"
