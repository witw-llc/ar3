"""The a8s registry, read-only, for code that must not import an a8s module.

a8s keeps the nodes it routes to in `<a8s home>/a8s.json`:

    {"agents": {NAME: {"root": DIR, "definition": PATH_OR_NAME, "vars": {...}, ...}},
     "aliases": {...}, "namespaces": {...}}

A program that must learn something about a node, such as the directory it was
registered with or the definition it runs, reads it here, by the node's name.
It never infers a node from its working directory or its environment. A
directory is not a node because it holds a file, and a variable is not proof of
who is calling.

The home resolves as a8s resolves it: `A8S_HOME`, else the XDG config home,
else the legacy `~/.a8s` (see `ar3.home.app_home`). Every read fails closed: a
registry that is missing, unreadable or not what a8s writes holds no node.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from ar3.home import app_home


def a8s_home() -> Path:
    """The directory that holds the a8s registry. It is not created."""
    return app_home("a8s", os.environ.get("A8S_HOME"), legacy=Path.home() / ".a8s")


def read() -> tuple[dict, dict]:
    """`(agents, aliases)` as the registry file holds them. Each is `{}` when the file is missing,
    cannot be read, is not JSON, or is not an object. An agent entry that is not an object is left out."""
    try:
        data = json.loads((a8s_home() / "a8s.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError):  # RuntimeError: no home directory, or JSON nested too deep
        return {}, {}
    if not isinstance(data, dict):
        return {}, {}
    agents, aliases = data.get("agents"), data.get("aliases")
    return (
        {name: info for name, info in agents.items() if isinstance(info, dict)} if isinstance(agents, dict) else {},
        aliases if isinstance(aliases, dict) else {},
    )


def definition_stem(raw: object) -> str:
    """The short name of a definition: the last part of its path, without `.json`. A node added with no
    definition runs `default`."""
    name = Path(str(raw or "default")).name
    return name[: -len(".json")] if name.endswith(".json") else name


def node_root(name: str, definition: str) -> Path | None:
    """The directory node `name` was registered with, when that node runs the definition called
    `definition`. None for any other case: no such node, a node of another kind, or a root that is not
    an absolute path (a relative one would mean the working directory).

    `name` is matched exactly, as a8s stores it, and a8s keeps names in lower case. A wake passes the name the
    message was addressed to: for a node addressed by its own name that is the stored one, and `ar3 tool --node`
    passes the stored name. An alias or a namespace address is no node, so a seat of that name finds none."""
    info = read()[0].get(name)
    if info is None or definition_stem(info.get("definition")) != definition:
        return None
    root = info.get("root")
    if not isinstance(root, str) or not os.path.isabs(root):
        return None
    return Path(root)


def _node_binding(root: str) -> str:
    blob = json.dumps({"root": str(root)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def node_secrets(name: str) -> dict[str, str]:
    """The stored `secret.<NAME>` map (`{NAME: value}`) for registered node `name`, read-only.

    Reads `<a8s home>/secrets.json` (`"nodes"` section) only when `name` is registered in `a8s.json`
    and the stored `bind` digest matches `name`'s registered `root`. Fails closed (`{}`) when `name`
    is not registered, `secrets.json` is absent or unreadable, or any stored section is malformed.
    """
    agents = read()[0]
    canon = None
    info = None
    query = (name or "").strip().lower()
    if not query:
        return {}
    for key, val in agents.items():
        if isinstance(key, str) and key.lower() == query:
            canon = key
            info = val
            break
    if canon is None or not isinstance(info, dict):
        return {}
    root = info.get("root")
    if not isinstance(root, str) or not root:
        return {}
    try:
        data = json.loads((a8s_home() / "secrets.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError):
        return {}
    if not isinstance(data, dict):
        return {}
    for section in ("remotes", "services", "nodes"):
        if section in data and not isinstance(data[section], dict):
            return {}
    nodes_map = data.get("nodes")
    if not isinstance(nodes_map, dict):
        return {}
    matches = [k for k in nodes_map if isinstance(k, str) and k.lower() == canon.lower()]
    if not matches:
        return {}
    expected_bind = _node_binding(root)
    merged: dict[str, str] = {}
    for match_key in matches:
        entry = nodes_map[match_key]
        if (
            not isinstance(entry, dict)
            or not set(entry) <= {"bind", "secrets"}
            or entry.get("bind") != expected_bind
            or not isinstance(entry.get("secrets"), dict)
        ):
            return {}
        for k, v in entry["secrets"].items():
            if (
                not isinstance(k, str)
                or not k
                or "=" in k
                or "\0" in k
                or k.upper() == "PATH"
                or not isinstance(v, str)
                or not v
                or "\0" in v
            ):
                return {}
            merged[k] = v
    return merged
