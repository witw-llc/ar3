#!/usr/bin/env python3
"""Count every user-facing surface as wired, deferred, or unaccounted.

The 1.0 bar says every config key, CLI verb and runbook section that parses is
either wired end to end with a test, or marked deferred in the surface where
the user meets it. There is no third category — and until something counts the
surfaces, "no third category" is a judgement instead of a number. This tool is
the count.

A surface item is:

  wired         a test names it as a string literal AND a docs page names it
  deferred      the text where the user meets it carries a deferral marker
  unaccounted   neither

The deferral marker convention, so that "marked deferred" is checkable:

  * In code — an argparse `help=`, a module docstring, or the comment on a
    key's declaration line — the words `deferred` or `not yet`, or a bare
    `#NNN` issue reference. The repo already rules that a number in source
    names work still to be done, so a verb whose help text cites an issue is
    a verb whose author said out loud that it is not finished.
  * In `docs/` — only the words `deferred` or `not yet`, on a line that names
    the item. Docs cite issues as references a reader follows, so `#NNN` in a
    doc page means nothing about deferral.

Repo-wide by nature, which is why it lives here. A checker that scans every
app while living in one app's tests stays green by not running, because the
per-PR workflow routes by path. `release.yml` runs this on every merge, and it
costs nothing to run by hand:

    tools/surface-audit.py              # a table per surface, then the counts
    tools/surface-audit.py --json       # the same report, machine-readable

Exit 0 means nothing is unaccounted for. Exit 1 lists what is, and each line
is a 1.0 to-do: wire it with a test, or say in its own surface that it waits.
`tools/surface-audit.allow` suppresses an item, and every entry there carries
the reason it is allowed.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SURFACES = ("cli", "config", "runbook")

# An app's CLI lives in one of these, by convention: the front door and k7e use
# `cli.py`, a8s splits `a8s.py` and `commands.py`, r4t uses `<app>.py`.
ENTRY_NAMES = ("cli.py", "commands.py", "main.py")

# A module-level table of accepted keys announces itself by name. Matching on
# the shape rather than on a hand-kept list of variables means a new key table
# is audited the day it is written, not the day someone remembers this file.
KEY_TABLE = re.compile(r"^_?[A-Z][A-Z0-9_]*?(KEYS|FIELDS|SECTIONS|SETTINGS|OPTIONS|KNOBS)$")

# A table whose name says the keys are gone declares rejections, not surfaces.
GONE_TABLE = re.compile(r"^_?GONE_")
# A table whose name says the keys are deferred IS the deferral marker: the
# parser that meets the user names the key and says it does not land yet.
DEFERRED_TABLE = re.compile(r"^_?DEFERRED_")

# a8s and k7e carry their verbs as data rather than as subparsers, so the table
# is a surface in its own right and not every verb reaches an `add_parser`.
VERB_TABLE = re.compile(r"^(COMMANDS|VERBS|SUBCOMMANDS|ALIASES)$")

DEFER_WORDS = re.compile(r"\bdeferred\b|\bnot yet\b", re.IGNORECASE)
DEFER_ISSUE = re.compile(r"(?<![\w/])#\d+\b")

SKIP_PARTS = {"_vendor", "__pycache__", ".venv", ".git", "node_modules"}


@dataclass
class Item:
    """One thing a user can type, set, or write, and where it is declared."""

    surface: str
    app: str
    name: str
    origin: str
    text: str = ""

    @property
    def key(self) -> str:
        return f"{self.surface}:{self.app}:{self.name}"


@dataclass
class Evidence:
    """What the tree says about a name, gathered once and asked many times.

    Both stores keep their groupings. A flat set of every string in a suite
    would let `rig` from one test and `run` from another combine into proof
    for `rig run`, a command neither test types, so the unit of evidence is
    the argv or call the strings sit in and the code span or fenced line the
    docs show them on.
    """

    test_literals: dict = field(default_factory=dict)
    test_groups: dict = field(default_factory=dict)
    docs_spans: list = field(default_factory=list)
    docs_lines: list = field(default_factory=list)


def is_skipped(path: Path) -> bool:
    return bool(SKIP_PARTS & set(path.parts))


def parse(path: Path):
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def apps(root: Path):
    apps_dir = root / "apps"
    if not apps_dir.is_dir():
        return
    for path in sorted(apps_dir.iterdir()):
        if path.is_dir() and not is_skipped(path):
            yield path


def entry_points(app: Path):
    for name in (*ENTRY_NAMES, f"{app.name}.py"):
        candidate = app / name
        if candidate.is_file():
            yield candidate


def const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def kwarg(call: ast.Call, name: str):
    for keyword in call.keywords:
        if keyword.arg == name:
            return const_str(keyword.value)
    return None


def target_name(node):
    """The single plain name a statement assigns to, or None."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    if len(targets) == 1 and isinstance(targets[0], ast.Name):
        return targets[0].id
    return None


def receiver(call: ast.Call):
    """`x.add_parser(...)` -> "x"."""
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id
    return None


def method(call: ast.Call):
    return call.func.attr if isinstance(call.func, ast.Attribute) else None


def cli_verbs(root: Path):
    """Every argparse subcommand, and every string key of a dispatch table.

    Nesting is resolved by following the variables: a parser variable knows the
    verb path it holds, a subparsers variable knows the parser it came from, so
    `engine list` reads as `engine list` and not as a second `list`.
    """
    for app in apps(root):
        for entry in entry_points(app):
            tree = parse(entry)
            if tree is None:
                continue
            yield from _verbs_in(tree, app.name, entry, root)


def _verbs_in(tree, app: str, entry: Path, root: Path):
    parser_path = {}  # variable -> verb path it holds ("" for the root parser)
    group_parent = {}  # add_subparsers() variable -> owning verb path
    origin_of = str(entry.relative_to(root))

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        name = target_name(node)
        if name is None:
            continue
        if isinstance(value.func, ast.Attribute) and value.func.attr == "ArgumentParser":
            parser_path[name] = ""
        elif isinstance(value.func, ast.Name) and value.func.id == "ArgumentParser":
            parser_path[name] = ""
        elif method(value) == "add_subparsers":
            group_parent[name] = parser_path.get(receiver(value), "")
        elif method(value) == "add_parser":
            verb = const_str(value.args[0]) if value.args else None
            if verb:
                parent = group_parent.get(receiver(value), "")
                parser_path[name] = f"{parent} {verb}".strip()

    seen = set()

    def emit(path, node, text=""):
        if path in seen:
            return None
        seen.add(path)
        return Item("cli", app, path, f"{origin_of}:{node.lineno}", text)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or method(node) != "add_parser":
            continue
        verb = const_str(node.args[0]) if node.args else None
        if not verb:
            continue
        parent = group_parent.get(receiver(node), "")
        # `help=_cmd_help("init")` is a call, not a literal; the description is
        # then the only prose the reader ever sees, so it is what gets scanned.
        text = kwarg(node, "help") or kwarg(node, "description") or ""
        for name in [verb, *_alias_names(node)]:
            item = emit(f"{parent} {name}".strip(), node, text)
            if item:
                yield item

    # `engine quota` is a positional with `choices=`, not a subparser. It is
    # still a verb the user types, so it is still a surface to account for.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or method(node) != "add_argument":
            continue
        positional = const_str(node.args[0]) if node.args else None
        if not positional or positional.startswith("-"):
            continue
        parent = parser_path.get(receiver(node))
        if not parent:
            continue
        for choice in _choice_names(node):
            item = emit(f"{parent} {choice}", node, kwarg(node, "help") or "")
            if item:
                yield item

    yield from _table_verbs(tree, app, origin_of, seen)


def _alias_names(call: ast.Call):
    for keyword in call.keywords:
        if keyword.arg == "aliases" and isinstance(keyword.value, (ast.List, ast.Tuple)):
            for element in keyword.value.elts:
                text = const_str(element)
                if text:
                    yield text


def _choice_names(call: ast.Call):
    for keyword in call.keywords:
        if keyword.arg == "choices" and isinstance(keyword.value, (ast.List, ast.Tuple)):
            for element in keyword.value.elts:
                text = const_str(element)
                if text:
                    yield text


def _table_verbs(tree, app: str, origin_of: str, seen: set):
    """A verb table carried as data — a8s and k7e declare their CLI this way.

    `COMMANDS` is a list of `(verb, usage, help)` rows; `ALIASES` maps an alias
    onto the verb it stands for. Both are annotated assignments as often as
    plain ones, so both statement forms are read.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        name = target_name(node)
        if not name or not VERB_TABLE.match(name) or node.value is None:
            continue
        for verb, text, lineno in _table_rows(node.value):
            if verb in seen:
                continue
            seen.add(verb)
            yield Item("cli", app, verb, f"{origin_of}:{lineno}", text)


def _table_rows(node):
    if isinstance(node, ast.Dict):
        for key in node.keys:
            text = const_str(key)
            if text:
                yield text, "", key.lineno
        return
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for element in node.elts:
            if isinstance(element, (ast.Tuple, ast.List)) and element.elts:
                verb = const_str(element.elts[0])
                blurb = " ".join(
                    filter(None, (const_str(part) for part in element.elts[1:]))
                )
                if verb:
                    yield verb, blurb, element.lineno
            else:
                verb = const_str(element)
                if verb:
                    yield verb, "", element.lineno


def config_keys(root: Path):
    """Every key named in a module-level table of accepted keys."""
    for app in apps(root):
        for path in sorted(app.rglob("*.py")):
            if is_skipped(path.relative_to(root)) or "tests" in path.parts:
                continue
            tree = parse(path)
            if tree is None:
                continue
            source = path.read_text(encoding="utf-8").splitlines()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                name = target_name(node)
                if not name or not KEY_TABLE.match(name):
                    continue
                if GONE_TABLE.match(name):
                    continue
                marker = "deferred" if DEFERRED_TABLE.match(name) else ""
                for key, lineno in _string_members(node.value):
                    text = " ".join(filter(None, (marker, _trailing_comment(source, lineno))))
                    origin = f"{path.relative_to(root)}:{lineno}"
                    yield Item("config", app.name, key, origin, text)


def _string_members(node):
    """The string constants a set/list/tuple/dict literal declares, with lines."""
    if isinstance(node, ast.Dict):
        for key in node.keys:
            text = const_str(key)
            if text:
                yield text, key.lineno
        return
    if isinstance(node, ast.Call) and node.args:
        node = node.args[0]
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        for element in node.elts:
            text = const_str(element)
            if text:
                yield text, element.lineno
            elif isinstance(element, ast.Call) and element.args:
                first = const_str(element.args[0])
                if first:
                    yield first, element.lineno


def _trailing_comment(source, lineno: int) -> str:
    line = source[lineno - 1] if 0 < lineno <= len(source) else ""
    _, _, comment = line.partition("#")
    return comment.strip()


def runbook_surfaces(root: Path):
    """The bundled agent definitions and the runbooks that ship with them."""
    definitions = root / "apps" / "a8s" / "definitions"
    if definitions.is_dir():
        for path in sorted(definitions.glob("*.json")):
            yield Item(
                "runbook",
                "a8s",
                path.stem,
                str(path.relative_to(root)),
                _json_text(path),
            )
    runbooks = root / "apps" / "r4t" / "runbooks"
    if runbooks.is_dir():
        for path in sorted(runbooks.glob("*.md")):
            head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:20])
            yield Item("runbook", "r4t", path.stem, str(path.relative_to(root)), head)


def _json_text(path: Path) -> str:
    try:
        return json.dumps(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return ""


CODE_SPAN = re.compile(r"`([^`\n]+)`")
FENCE = re.compile(r"^\s*```")


def gather(root: Path) -> Evidence:
    evidence = Evidence()
    shared = _literals(root / "tests")
    for app in apps(root):
        groups = _literals(app / "tests") + shared
        evidence.test_groups[app.name] = groups
        evidence.test_literals[app.name] = set().union(*groups) if groups else set()
    evidence.docs_spans, evidence.docs_lines = _docs(root / "docs")
    return evidence


def _literals(tests: Path) -> list:
    """Every string a test spells out, minus the docstrings, kept in groups.

    A test that never names the verb it drives is a test that keeps passing
    when the verb is deleted, which is exactly the wiring this counts. A
    docstring is excluded on purpose: "`a8s ps` lists running nodes" says what
    a test is about, and would still read true after `ps` stopped working.

    A group is one call's arguments, or one statement where no call encloses
    them — the span a reader would point at to say "this test types that
    command". JSON fixtures group by line, the same span a reader sees.
    """
    groups = []
    if not tests.is_dir():
        return groups
    for path in sorted(tests.rglob("*.py")):
        if is_skipped(path):
            continue
        tree = parse(path)
        if tree is None:
            continue
        found = {}
        _group_strings(tree, id(tree), found, _docstring_nodes(tree))
        groups.extend(found.values())
    for path in sorted(tests.rglob("*.json")):
        if is_skipped(path):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            found = set(re.findall(r'"([^"\n]{1,80})"', line))
            if found:
                groups.append(found)
    return groups


def _group_strings(root, group, found: dict, prose: set):
    """Bucket every string constant under the innermost call or statement."""
    stack = [(root, group)]
    while stack:
        node, group = stack.pop()
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str) and id(node) not in prose:
                found.setdefault(group, set()).add(node.value.strip())
            continue
        if isinstance(node, (ast.stmt, ast.Call)):
            group = id(node)
        stack.extend((child, group) for child in ast.iter_child_nodes(node))


def _docstring_nodes(tree) -> set:
    holders = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    prose = set()
    for node in ast.walk(tree):
        if not isinstance(node, holders) or not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                prose.add(id(first.value))
    return prose


def _docs(docs: Path):
    """The token set of every code span and every fenced line, span by span.

    A fenced line is one command the reader could copy; an inline span is one
    thing the prose puts in backticks. Either shows a whole command or it
    does not, and only the whole command is evidence that the docs name it.
    """
    spans = []
    lines = []
    if not docs.is_dir():
        return spans, lines
    for path in sorted(docs.rglob("*.md")):
        fenced = False
        for line in path.read_text(encoding="utf-8").splitlines():
            lines.append(line)
            if FENCE.match(line):
                fenced = not fenced
                continue
            if fenced:
                spans.append(set(re.findall(r"[\w./-]+", line)))
                continue
            for span in CODE_SPAN.findall(line):
                spans.append(set(re.findall(r"[\w./-]+", span)))
    return spans, lines


def word(name: str) -> re.Pattern:
    return re.compile(rf"(?<![\w./-]){re.escape(name)}(?![\w./-])")


def documented(item: Item, evidence: Evidence) -> bool:
    """One code span or one fenced line shows the whole command."""
    parts = item.name.split()
    return any(all(part in span for part in parts) for span in evidence.docs_spans)


def tested(item: Item, evidence: Evidence) -> bool:
    """A test names the item exactly, or spells out the command that reaches it.

    The exact match is the argv element a test passes in. The phrase match
    catches `assert "ar3 doctor" in out`. The last form is `["rig", "run"]` —
    the parts in one argv, which is how a test types a nested command. Nothing
    looser: `rig` in one test and `run` in another are two tests that both keep
    passing after `rig run` is deleted, so they are not evidence that it works.
    """
    literals = evidence.test_literals.get(item.app, set())
    if item.name in literals:
        return True
    parts = item.name.split()
    phrases = [f"{item.app} {item.name}"]
    if len(parts) > 1:
        phrases.append(item.name)
    patterns = [word(phrase) for phrase in phrases]
    if any(p.search(text) for text in literals for p in patterns):
        return True
    groups = evidence.test_groups.get(item.app, ())
    return any(all(part in group for part in parts) for group in groups)


def deferred(item: Item, evidence: Evidence) -> bool:
    if DEFER_WORDS.search(item.text) or DEFER_ISSUE.search(item.text):
        return True
    pattern = word(item.name.split()[-1])
    return any(DEFER_WORDS.search(line) and pattern.search(line) for line in evidence.docs_lines)


def classify(item: Item, evidence: Evidence) -> str:
    if tested(item, evidence) and documented(item, evidence):
        return "wired"
    if deferred(item, evidence):
        return "deferred"
    return "unaccounted"


def read_allowlist(path: Path):
    """`surface:app:name  # why`. The reason is the point; a bare key is a bug."""
    allowed, malformed = {}, []
    if not path.is_file():
        return allowed, malformed
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, why = line.partition("#")
        key, why = key.strip(), why.strip()
        if not why:
            malformed.append((lineno, key))
            continue
        allowed[key] = why
    return allowed, malformed


def audit(root: Path):
    evidence = gather(root)
    items = [
        *cli_verbs(root),
        *config_keys(root),
        *runbook_surfaces(root),
    ]
    seen, unique = set(), []
    for item in items:
        if item.key in seen:
            continue
        seen.add(item.key)
        unique.append(item)
    return [(item, classify(item, evidence)) for item in sorted(unique, key=lambda i: i.key)]


def report(rows, allowed, malformed, as_json: bool) -> int:
    unaccounted = [i for i, verdict in rows if verdict == "unaccounted"]
    blocking = [i for i in unaccounted if i.key not in allowed]
    stale = sorted(set(allowed) - {i.key for i in unaccounted})

    if as_json:
        print(json.dumps({
            "items": [
                {
                    "surface": i.surface,
                    "app": i.app,
                    "name": i.name,
                    "origin": i.origin,
                    "verdict": v,
                    "allowed": i.key in allowed,
                }
                for i, v in rows
            ],
            "counts": _counts(rows),
            "blocking": [i.key for i in blocking],
            "stale_allowlist": stale,
            "malformed_allowlist": [key for _, key in malformed],
        }, indent=2))
    else:
        _print_tables(rows, allowed)
        for item in blocking:
            print(f"unaccounted  {item.key}  ({item.origin})")
        for lineno, key in malformed:
            print(f"allowlist:{lineno}  no reason given for {key or 'a blank entry'}")
        for key in stale:
            print(f"allowlist    {key} is accounted for now — drop the line")

    return 1 if blocking or malformed else 0


def _counts(rows):
    counts = {}
    for item, verdict in rows:
        bucket = counts.setdefault(item.surface, {"wired": 0, "deferred": 0, "unaccounted": 0})
        bucket[verdict] += 1
    return counts


def _print_tables(rows, allowed):
    counts = _counts(rows)
    width = max((len(i.key) for i, _ in rows), default=10)
    for surface in SURFACES:
        subset = [(i, v) for i, v in rows if i.surface == surface]
        if not subset:
            continue
        print(f"\n{surface}")
        for item, verdict in subset:
            mark = " (allowed)" if verdict == "unaccounted" and item.key in allowed else ""
            print(f"  {item.key:<{width}}  {verdict}{mark}")
        tally = counts[surface]
        print(f"  -- {tally['wired']} wired, {tally['deferred']} deferred, "
              f"{tally['unaccounted']} unaccounted")
    total = {"wired": 0, "deferred": 0, "unaccounted": 0}
    for tally in counts.values():
        for key, value in tally.items():
            total[key] += value
    print(f"\n{sum(total.values())} surfaces: {total['wired']} wired, "
          f"{total['deferred']} deferred, {total['unaccounted']} unaccounted")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="tree to audit")
    parser.add_argument("--allow", type=Path, help="allowlist file")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(argv)

    allow_path = args.allow or (args.root / "tools" / "surface-audit.allow")
    allowed, malformed = read_allowlist(allow_path)
    return report(audit(args.root), allowed, malformed, args.json)


if __name__ == "__main__":
    sys.exit(main())
