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
