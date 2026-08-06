"""a8s registry — ~/.config/a8s/a8s.json I/O and name resolution.

Schema:
  {
    "agents":     {"<NAME>": {"root": "...", "definition": "...?", "safe_dirs": ["..."], "vars": {"KEY": "..."}}},
    "aliases":    {"<ALIAS>": ["<NAME-or-ALIAS>", ...]},
    "namespaces": {"<PREFIX>": "<AGENT>"}
  }
  `safe_dirs` — optional extra directories (absolute paths) where FILE
  attachments may originate at routing time, in addition to `root`.
  `vars` — optional per-node a8s variables (`a8s vars`); expanded as `$KEY` in
  definition argv. Not OS environment variables.
  `namespaces` — prefix routing: a recipient `<PREFIX>:<sub-address>`
  routes to the single bound agent with the full address preserved in `to`.
Aliases are disjoint from both agents and namespaces. A namespace prefix may
match the name of the agent it binds to (a node owning its own namespace);
it may not match an alias or a *different* agent (`cmd_add` / `cmd_namespace`
reject those).

Also hosts the read-only marker-file scan used by `cmd_discover` and the
auto-detect path in `cmd_add`.
"""
from __future__ import annotations

import json
from pathlib import Path

from core import (
    MARKER_FILES,
    NAME_RE,
    Participant,
    canonical_name,
    registry_path,
)


# ---------- registry I/O ----------

_REGISTRY_SECTIONS = ("agents", "aliases", "namespaces", "namespace_options")


def _empty_registry() -> dict:
    return {section: {} for section in _REGISTRY_SECTIONS}


def _load_raw_registry() -> dict:
    p = registry_path()
    if not p.is_file():
        return _empty_registry()
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return _empty_registry()
    if not isinstance(data, dict):
        return _empty_registry()
    raw = {}
    for section in _REGISTRY_SECTIONS:
        value = data.get(section)
        raw[section] = value if isinstance(value, dict) else {}
    return raw


def _save_raw_registry(data: dict) -> None:
    payload = {section: data.get(section) or {} for section in _REGISTRY_SECTIONS}
    registry_path().write_text(json.dumps(payload, indent=2, sort_keys=True))


def load_registry() -> dict:
    """Return just the agents section."""
    return _load_raw_registry()["agents"]


def save_registry(agents: dict) -> None:
    """Write the agents section, preserving the existing aliases section."""
    raw = _load_raw_registry()
    raw["agents"] = agents
    _save_raw_registry(raw)


def load_aliases() -> dict:
    return _load_raw_registry()["aliases"]


def save_aliases(aliases: dict) -> None:
    raw = _load_raw_registry()
    raw["aliases"] = aliases
    _save_raw_registry(raw)


def load_namespaces() -> dict:
    return _load_raw_registry()["namespaces"]


def save_namespaces(namespaces: dict) -> None:
    raw = _load_raw_registry()
    raw["namespaces"] = namespaces
    _save_raw_registry(raw)


def load_namespace_options() -> dict:
    return _load_raw_registry()["namespace_options"]


def save_namespace_options(options: dict) -> None:
    raw = _load_raw_registry()
    raw["namespace_options"] = options
    _save_raw_registry(raw)


def opaque_prefixes() -> set[str]:
    """Lowercased prefixes bound with `--opaque` — presentation policy, kept
    beside the routing map rather than inside it so every consumer of
    `namespaces` keeps reading `{prefix: agent}`."""
    return {
        p.lower()
        for p, opts in load_namespace_options().items()
        if isinstance(opts, dict) and opts.get("opaque")
    }


# ---------- lookups ----------

def split_namespace_address(query: str) -> tuple[str, str] | None:
    """Split a `<prefix>:<sub-address>` recipient on the FIRST colon. Returns
    `(canonical_prefix, sub_address)`, or None when `query` has no colon.

    The sub-address is opaque to a8s — further colons belong to it
    (`acme:ops:phil` → prefix `acme`, sub `ops:phil`). Raises ValueError on a
    malformed address (empty sub-address, or a prefix outside the NAME_RE
    grammar) — a colon address can never name an agent or alias, so a bad one
    is a malformed recipient, not an unknown one."""
    q = (query or "").strip()
    if ":" not in q:
        return None
    prefix, _, sub = q.partition(":")
    if not sub:
        raise ValueError(f"namespace address {query!r} has an empty sub-address")
    try:
        return canonical_name(prefix), sub
    except ValueError:
        raise ValueError(f"namespace address {query!r} has an invalid prefix") from None

def resolve_recipient(query: str) -> tuple[str, dict] | None:
    """Look up an agent by exact (case-insensitive) name. Aliases are NOT
    resolved here — that's `resolve_name()`'s job for fan-out."""
    reg = load_registry()
    q = query.strip().lower()
    for name, info in reg.items():
        if name.lower() == q:
            return name, info
    return None


def sender_from_cwd() -> tuple[str, dict] | None:
    reg = load_registry()
    cwd = Path.cwd().resolve()
    for parent in [cwd, *cwd.parents]:
        for name, info in reg.items():
            try:
                root = Path(info.get("root", "")).resolve()
            except (OSError, RuntimeError):
                continue
            if root == parent:
                return name, info
    return None


def resolve_name(query: str) -> tuple[str, list[str]]:
    """Expand `query` into a flat list of agent names.

    Returns (kind, agent_names) where kind ∈ {"agent", "alias", "namespace"}.
    For an alias, members are walked recursively; cycles raise ValueError.
    Unknown names raise KeyError.

    A colon in `query` makes it a namespace address: the prefix before
    the first colon resolves via the `namespaces` map to its single bound
    agent. A bare `query` with no colon that matches a bound prefix is also
    a namespace address (delivered with `to` = the prefix alone — the node
    self-routes, e.g. r4t sends bare namespace traffic to the roster leader).
    The grammar forbids colons in agent and alias names, so the paths can't
    overlap. A malformed address raises ValueError; an unbound prefix (or a
    binding to a since-removed agent — same dangling shape as an alias member
    that no longer exists) raises KeyError.

    A diamond (the same alias reached via two distinct parents) is NOT a cycle:
    `path` tracks aliases currently on the recursion stack; `seen_aliases`
    short-circuits second-and-later visits via different paths. Agents dedup
    via `out_names` membership.
    """
    raw = _load_raw_registry()
    agents = raw["agents"]
    aliases = raw["aliases"]
    agent_lookup = {n.lower(): n for n in agents}
    alias_lookup = {n.lower(): n for n in aliases}
    namespace_lookup = {n.lower(): (n, v) for n, v in raw["namespaces"].items()}
    split = split_namespace_address(query)
    if split is not None:
        prefix, _sub = split
        bound = namespace_lookup.get(prefix)
        if bound is None:
            raise KeyError(query)
        _prefix_canon, bound_agent = bound
        bound_key = str(bound_agent).strip().lower()
        if bound_key not in agent_lookup:
            raise KeyError(f"namespace {prefix!r} is bound to unknown agent {bound_agent!r}")
        return "namespace", [agent_lookup[bound_key]]
    q = query.strip().lower()
    # Namespace beats agent: when a node owns a namespace matching its own name
    # (e.g. agent `s1l` bound to prefix `s1l`), both routes land on the
    # same node — the namespace path just delivers `to` as the bare prefix so
    # the node self-routes, which is exactly what a bare `tell s1l` should do.
    if q in namespace_lookup:
        _prefix_canon, bound_agent = namespace_lookup[q]
        bound_key = str(bound_agent).strip().lower()
        if bound_key not in agent_lookup:
            raise KeyError(f"namespace {q!r} is bound to unknown agent {bound_agent!r}")
        return "namespace", [agent_lookup[bound_key]]
    if q in agent_lookup:
        return "agent", [agent_lookup[q]]
    if q in alias_lookup:
        out_names: list[str] = []
        path: set[str] = set()           # aliases currently being recursed
        seen_aliases: set[str] = set()   # aliases already fully expanded

        def walk(name: str) -> None:
            key = name.lower()
            if key in agent_lookup:
                resolved = agent_lookup[key]
                if resolved not in out_names:
                    out_names.append(resolved)
                return
            if key in alias_lookup:
                if key in path:
                    raise ValueError(f"alias cycle detected at {name!r}")
                if key in seen_aliases:
                    return  # diamond — already expanded via another parent
                path.add(key)
                try:
                    for member in aliases[alias_lookup[key]]:
                        walk(str(member))
                finally:
                    path.discard(key)
                seen_aliases.add(key)
                return
            raise KeyError(f"alias {alias_lookup[q]!r} references unknown name {name!r}")

        walk(alias_lookup[q])
        return "alias", out_names
    raise KeyError(query)


def find_participant(parts: list[Participant], query: str) -> Participant | None:
    q = query.strip().lower()
    for p in parts:
        if p.name.lower() == q:
            return p
    return None


def participants_from_registry() -> list[Participant]:
    """Build Participants from the registry — the single source of truth for
    which agents exist. No filesystem walk; explicit `a8s add` is required."""
    reg = load_registry()
    parts: list[Participant] = []
    for name, info in reg.items():
        root_str = info.get("root", "")
        if not root_str:
            continue
        try:
            root = Path(root_str).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        safe_dirs: list[Path] = []
        raw_dirs = info.get("safe_dirs") or []
        if isinstance(raw_dirs, list):
            for item in raw_dirs:
                if not isinstance(item, str) or not item.strip():
                    continue
                try:
                    safe_dirs.append(Path(item).expanduser().resolve())
                except (OSError, RuntimeError):
                    continue
        parts.append(
            Participant(
                name=name,
                root=root,
                safe_dirs=tuple(safe_dirs),
                outbox=_participant_outbox(name, root),
                files=_participant_files(name, root),
                inbox=_participant_inbox(name, root),
            )
        )
    return parts


def _participant_inbox(name: str, root: Path) -> Path:
    from definitions import resolve_inbox_dir_for_agent

    return resolve_inbox_dir_for_agent(name, root)


def _participant_files(name: str, root: Path) -> Path:
    from definitions import resolve_files_dir_for_agent

    return resolve_files_dir_for_agent(name, root)


def _participant_outbox(name: str, root: Path) -> Path:
    from definitions import resolve_outbox_dir_for_agent

    return resolve_outbox_dir_for_agent(name, root)


# ---------- discovery (suggestions only) ----------

def parse_name(marker_path: Path) -> str | None:
    try:
        with marker_path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.lstrip()
                if not line.startswith("#"):
                    continue
                rest = line.lstrip("#").strip()
                m = NAME_RE.match(rest)
                return m.group(0) if m else None
    except OSError:
        return None
    return None


def _scan_for_markers(root: Path) -> list[tuple[str, str, Path]]:
    """Walk `root` (and its immediate children) for marker files. Returns
    `(name, kind, dir)` triples. Read-only; used by `a8s discover`."""
    candidates: list[Path] = [root]
    try:
        candidates.extend(p for p in sorted(root.iterdir()) if p.is_dir())
    except OSError:
        pass
    found: list[tuple[str, str, Path]] = []
    for d in candidates:
        for marker_name, kind in MARKER_FILES.items():
            marker = d / marker_name
            if not marker.is_file():
                continue
            name = parse_name(marker)
            if not name:
                continue
            found.append((name, kind, d.resolve()))
            break
    return found
