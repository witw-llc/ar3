"""The runbook — one `r4t.md` that says what a team is.

A runbook sits at the node directory root and replaces the scatter of
`ROSTER.md` + `MISSION.md` + `CHARTER.md` + `rigs.json` + `r4t-org.json` with
one markdown file a person reads top to bottom. YAML frontmatter carries the
file's own settings; the body is a CLOSED set of six H2 sections:

    ## Mission   ## Charter   ## Roster   ## Cells   ## Rigs   ## Rituals

An unknown or repeated H2 is a hard error naming the set — the typo-catcher
for the whole document. Prose under a collection section is the reader's
orientation text and is ignored; only `###` blocks are structural.

One block grammar serves members, cells, rigs and rituals: the leading run of
`- **Key:** value` bullets are its fields, everything after them is prose the
model reads verbatim. The bold is optional (`- Key: value` parses identically)
and keys are case-, space- and underscore-insensitive, so `Allowed tools` and
`allowed_tools` are one key.

A member is complete with one field. `Engine: claude --model opus` is the
inline style — a `r4t engine <id> run` invocation minus the prompt, so a member
that misbehaves is debugged by copying its own line out of the file. `Rig:` is
the class, and both are written in the same property language, which is what
makes promoting one to the other a copy and a paste. A member carrying both is
refused: that is a person mid-promotion who does not know which one is live.

**Rig precedence.** A rig defined in `## Rigs` SHADOWS a machine rig of the
same name in `~/.config/r4t/rigs.json`, whole-block — never field-merged. The
runbook is the reproducible statement of how the roster runs; a runbook rig
that silently inherited a permission stance from a machine rig of the same
name would be the opposite of reproducible.

**The trust ceiling.** A `## Rigs` block may name a permission stance, and the
file it sits in is checked in. So the runbook proposes and the machine caps: a
stance above the node's out-of-repo ceiling (`auto` unless `r4t add --trust`
raised it) fails that rig closed, here, every time the runbook loads. A repo
cannot raise its own permissions by editing itself.

**Inheritance.** `extends:` in frontmatter names the base — a built-in
(`apps/r4t/runbooks/*.md`, resolved by name like an a8s bundled definition) or
a path relative to the file. Two rules, one per document level: frontmatter
merges per key, and an H2 section REPLACES the base's whole. Chains compose,
which is also the file-splitting answer: a file that names only its own
sections adds them to the chain instead of overriding. Depth cap 5, and a
cycle is a hard error naming the loop.

Three things the design specifies and v1 does not carry, refused by name so a
user hears "deferred" rather than "unknown": H3-level block merge, the
`Remove:` tombstone, and a multi-file `r4t/` convention. The `extends:` chain
is the split.

**Variables.** `${VAR}`, `${VAR:-default}` and `${VAR:?message}` resolve from
the node's a8s vars, in field values and prose only — never in a heading and
never in frontmatter, so the shape of the file is readable without knowing a
single variable. Interpolation runs BEFORE merging, so a resolved runbook is
plain markdown with no substitution left in it. An unset variable with no
default is a hard error at load: the same fail-closed rule a8s applies to its
own mailbox path fields, for the same reason — a silent empty string puts a
roster somewhere nobody asked for.

**The mission is the mutable piece.** A node var named `MISSION` is treated as
a synthesized `## Mission` section at the highest precedence layer, so the
override chain is one mechanism with three delivery points: the built-in's
mission, the user's own, then the var. `r4t runbook show --resolved --sources`
prints which layer every section came from.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from rig import (
    HARNESS_PRESETS,
    PERMISSION_MODES,
    A8S_PY,
    Rig,
    build_preset_invoke,
    ceiling_refusal,
    machine_ceiling,
    permission_rank,
    preset_names,
    rig_from_spec,
)
from roster import Member, Roster, clean_field, member_from_fields, parse_bool_field

RUNBOOK_NAME = "r4t.md"
BUILTIN_DIR = Path(__file__).resolve().parent / "runbooks"
MAX_EXTENDS_DEPTH = 5

MISSION_VAR = "MISSION"
VAR_ENV_PREFIX = "A8S_VAR_"

SECTIONS = ("Mission", "Charter", "Roster", "Cells", "Rigs", "Rituals")
COLLECTION_SECTIONS = ("Roster", "Cells", "Rigs", "Rituals")
_SECTION_BY_KEY = {name.lower(): name for name in SECTIONS}

# Twin of `NAME_RE` in apps/a8s/core.py. r4t must not import a8s's modules
# (flat module names collide), so the charset is pinned here and
# test_runbook.py asserts the twins stay identical. It is the reason a colon
# cannot appear in a member or cell name: the colon is the address separator,
# and `tell node:member` has to parse one way.
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")

H2_RE = re.compile(r"^##(?!#)\s+(.*?)\s*$")
H3_RE = re.compile(r"^###(?!#)\s+(.*?)\s*$")
HEADING_RE = re.compile(r"^#{1,6}\s")
FIELD_RE = re.compile(r"^-\s+\*{0,2}([A-Za-z][A-Za-z0-9 _-]*?)\*{0,2}\s*:\s*\*{0,2}\s*(.*?)\s*$")
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:-|:\?)([^}]*))?\}")
BUDGET_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*per\s*hour\s*,\s*max\s*(\d+(?:\.\d+)?)$", re.IGNORECASE
)
SCHEDULE_RE = re.compile(
    r"^(?:every\s+\d+[smhd]"
    r"|daily\s+\d{1,2}:\d{2}"
    r"|weekdays\s+\d{1,2}:\d{2}"
    r"|weekly\s+(?:mon|tue|wed|thu|fri|sat|sun)\s+\d{1,2}:\d{2}"
    r"|monthly\s+\d{1,2}\s+\d{1,2}:\d{2}"
    r"|on\s+idle)$",
    re.IGNORECASE,
)
SCHEDULE_HINT = (
    "When must be one of: `every 30m` / `every 4h`, `daily 09:00`, "
    "`weekdays 09:00`, `weekly mon 09:00`, `monthly 1 09:00`, or `on idle` "
    "(machine-local time; there is no cron form)"
)

ENGINE_FLAGS = ("--model", "--permissions", "--allowed-tools", "--timeout")

MEMBER_KEYS = {
    "engine", "rig", "leader", "ingress", "cell", "lead", "workdir", "role",
    "continue", "knowledge", "prosereply", "framing", "reinforce",
}
CELL_KEYS = {"lead", "ingress"}
RIG_KEYS = {
    "engine", "allowedtools", "rigbudget", "memberbudget", "env", "mcp",
    "echo", "echomax", "maxsends", "history",
}
RITUAL_KEYS = {"when", "to", "budget"}

# Every frontmatter key anything actually reads: `name`/`extends`/`workdir`
# here, the rest in org.py's `_parse_settings` (comms, egress,
# leader_sees_lateral, priority_senders, run_as, container, container_args) —
# one tuple both `_cross_check` and the options sheet's own test import, so
# the accepted set can never drift between the two. Frontmatter is the org
# seam and org.py reads it ad hoc rather than through a schema, so an unknown
# key here is a WARNING, not a hard error the way an unknown block key is: a
# forward-compat key from a newer runbook must not fail an older parser
# closed, and there is nothing here worth failing a turn over.
FRONTMATTER_KEYS = {
    "name", "extends", "workdir", "comms", "egress",
    "leader_sees_lateral", "priority_senders",
    "run_as", "container", "container_args",
}

# Fields a previous format carried, refused by name with what replaced them.
GONE_KEYS = {
    "human": (
        "Human: is gone — the node is the apex. The owner is an ordinary a8s "
        "agent outside the roster; mail reaches him with `tell`"
    ),
    "address": (
        "Address: is gone with the seat doorbell — mail crossing the wall is "
        "a8s's job, not a member field"
    ),
    "status": "Status: is gone — members carry no marker",
    "flush": (
        "Flush: is not a field — the idle window rides Continue: "
        "(try: Continue: 15m)"
    ),
    "fallback": "Fallback: is gone — the knob is now ProseReply: (try: ProseReply: off)",
    "mandate": "Mandate: is gone — the one-line job title is Role:",
}

# Design surface v1 deliberately does not carry. Named as deferred so a reader
# who copied it out of the design page hears why, not "unknown key".
DEFERRED_KEYS = {
    "remove": (
        "Remove: is deferred — v1 has no tombstones and no H3-level merge. "
        "A section replaces whole, so state the blocks you want"
    ),
    "budget": (
        "Budget: on a cell is deferred — the cell bucket is roster-wide today "
        "(cell_budget_max / cell_budget_earn_per_hour in rigs.json)"
    ),
    "concurrency": (
        "Concurrency: is gone — one live turn per node is the contract, and a "
        "knob that can only hold one value invites asking what a second does"
    ),
}


class RunbookError(Exception):
    pass


# --- the file on disk --------------------------------------------------------


@dataclass
class Block:
    """One `###` entry: its fields, its prose payload, and where it came from."""

    name: str
    fields: dict[str, list[str]] = field(default_factory=dict)
    lines: dict[str, int] = field(default_factory=dict)
    prose: str = ""
    text: str = ""
    line: int = 0

    def one(self, key: str) -> str:
        values = self.fields.get(key) or [""]
        return values[0]


@dataclass
class Section:
    name: str
    text: str = ""
    prose: str = ""
    blocks: list[Block] = field(default_factory=list)
    source: str = ""
    line: int = 0

    def block(self, name: str) -> Block | None:
        key = name.strip().lower()
        for b in self.blocks:
            if b.name.lower() == key:
                return b
        return None


@dataclass
class RunbookFile:
    path: Path
    label: str
    frontmatter: dict = field(default_factory=dict)
    preamble: str = ""
    sections: dict[str, Section] = field(default_factory=dict)


# --- the resolved runbook ----------------------------------------------------


@dataclass
class Cell:
    name: str
    lead: str = ""
    # Whether `tell <node>:<cell>` from outside the roster delivers. Parsed and
    # carried; one post forked to a whole cell is #183, and until it lands a
    # cell address is refused by name rather than silently delivered somewhere.
    ingress: bool = False
    prose: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class Ritual:
    name: str
    when: str = ""
    to: str = ""
    budget: str = "charge"
    prompt: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class Runbook:
    path: Path
    root: Path
    name: str
    frontmatter: dict = field(default_factory=dict)
    sections: dict[str, Section] = field(default_factory=dict)
    preamble: str = ""
    roster: Roster | None = None
    rigs: dict[str, Rig] = field(default_factory=dict)
    cells: dict[str, Cell] = field(default_factory=dict)
    rituals: dict[str, Ritual] = field(default_factory=dict)
    chain: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def mission(self) -> str:
        section = self.sections.get("Mission")
        return section.text.strip() if section else ""

    @property
    def charter(self) -> str:
        section = self.sections.get("Charter")
        return section.text.strip() if section else ""

    def source_of(self, section: str) -> str:
        found = self.sections.get(section)
        return found.source if found else ""


# --- a8s node vars -----------------------------------------------------------


# One read per node per process. A wake is a short-lived process and a
# runbook is parsed several times inside it (the roster, the mission, the
# charter), so re-asking a8s each time would spend a subprocess to learn what
# cannot have changed. `a8s vars set` lands on the next wake, which is the
# same latency the registry already has for argv.
_VARS_CACHE: dict[str, dict[str, str]] = {}


def clear_vars_cache() -> None:
    _VARS_CACHE.clear()


class NodeVars:
    """The node's a8s vars, read on demand.

    Asked of a8s rather than off disk, the same reason `notify.visible_a8s_names`
    does: the registry's shape is pre-v1 and may be rebuilt, but `a8s vars` is
    the contract. `A8S_VAR_<KEY>` in the environment wins over the registry —
    it is the interface a8s exports on wake, and it means a caged turn that
    never sees the registry still resolves what it was given.

    `values` skips the lookup entirely, which is what a caller with the vars
    already in hand passes.
    """

    def __init__(self, node: str | None = None, values: dict[str, str] | None = None):
        self.node = (node or "").strip()
        self._values: dict[str, str] | None = dict(values) if values is not None else None

    def _registry(self) -> dict[str, str]:
        if self._values is not None:
            return self._values
        if not self.node:
            return {}
        if self.node not in _VARS_CACHE:
            _VARS_CACHE[self.node] = _read_a8s_vars(self.node)
        return _VARS_CACHE[self.node]

    def get(self, name: str) -> str | None:
        env = os.environ.get(VAR_ENV_PREFIX + name.upper())
        if env is not None:
            return env
        return self._registry().get(name.upper())


def _read_a8s_vars(node: str) -> dict[str, str]:
    try:
        res = subprocess.run(
            [sys.executable, str(A8S_PY), "vars", node],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if res.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in (res.stdout or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and NAME_RE.fullmatch(parts[0]):
            out[parts[0].upper()] = parts[1].strip()
    return out


# --- parsing -----------------------------------------------------------------


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _scalar(raw: str) -> object:
    value = raw.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_strip_quotes(part.strip()) for part in inner.split(",") if part.strip()]
    lowered = value.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    return _strip_quotes(value)


def _split_frontmatter(text: str, path: Path) -> tuple[dict, list[str], int]:
    """(frontmatter, body lines, 1-based line number of the first body line)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, lines, 1
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            data: dict = {}
            for offset, raw in enumerate(lines[1:index], start=2):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" not in line:
                    raise RunbookError(
                        f"{path}:{offset}: frontmatter line is not `key: value` "
                        f"({raw.strip()!r})"
                    )
                key, _, value = line.partition(":")
                data[key.strip().lower()] = _scalar(value)
            return data, lines[index + 1:], index + 2
    raise RunbookError(f"{path}:1: frontmatter opened with `---` and never closed")


def _normalize_key(key: str) -> str:
    return re.sub(r"[\s_-]+", "", key).lower()


def _parse_block(name: str, body: list[str], start: int) -> Block:
    """`body` is the lines under a `### <name>` heading, `start` the 1-based
    line number of the first of them. Fields are the leading run of bullets;
    everything from the first other non-blank line on is the prose payload."""
    block = Block(name=name, line=start - 1)
    block.text = "\n".join([f"### {name}", *body]).rstrip()
    index = 0
    while index < len(body):
        line = body[index]
        if not line.strip():
            index += 1
            continue
        if not line.lstrip().startswith("-"):
            break
        match = FIELD_RE.match(line.strip())
        if not match:
            break
        key = _normalize_key(match.group(1))
        block.fields.setdefault(key, []).append(clean_field(match.group(2)))
        block.lines.setdefault(key, start + index)
        index += 1
    while index < len(body) and not body[index].strip():
        index += 1
    block.prose = "\n".join(body[index:]).strip()
    return block


def parse_file(text: str, path: Path, label: str) -> RunbookFile:
    """One runbook file, structurally. No merging, no interpolation, no
    semantics — just frontmatter, preamble, sections and their blocks."""
    frontmatter, lines, first = _split_frontmatter(text, path)
    out = RunbookFile(path=path, label=label, frontmatter=frontmatter)

    preamble: list[str] = []
    section: Section | None = None
    section_lines: list[str] = []
    block_name: str | None = None
    block_lines: list[str] = []
    block_start = 0
    section_prose: list[str] = []

    def close_block() -> None:
        nonlocal block_name, block_lines, block_start
        if block_name is not None and section is not None:
            section.blocks.append(_parse_block(block_name, block_lines, block_start))
        block_name = None
        block_lines = []

    def close_section() -> None:
        nonlocal section, section_lines, section_prose
        close_block()
        if section is not None:
            section.text = "\n".join(section_lines).strip()
            section.prose = "\n".join(section_prose).strip()
            out.sections[section.name] = section
        section = None
        section_lines = []
        section_prose = []

    for offset, line in enumerate(lines):
        lineno = first + offset
        head2 = H2_RE.match(line)
        if head2:
            close_section()
            raw = head2.group(1).strip()
            canonical = _SECTION_BY_KEY.get(raw.lower())
            if canonical is None:
                raise RunbookError(
                    f"{path}:{lineno}: unknown section `## {raw}` — a runbook has "
                    f"exactly six: {', '.join(SECTIONS)}"
                )
            if canonical in out.sections:
                raise RunbookError(
                    f"{path}:{lineno}: `## {canonical}` appears twice — which one "
                    f"wins must never be a question"
                )
            section = Section(name=canonical, source=label, line=lineno)
            continue
        if section is None:
            preamble.append(line)
            continue
        section_lines.append(line)
        head3 = H3_RE.match(line)
        if head3 and section.name in COLLECTION_SECTIONS:
            close_block()
            block_name = head3.group(1).strip()
            block_start = lineno + 1
            continue
        if block_name is None:
            section_prose.append(line)
        else:
            block_lines.append(line)
    close_section()
    out.preamble = "\n".join(preamble).strip()
    return out


# --- interpolation -----------------------------------------------------------


def interpolate(lines: list[str], path: Path, first_line: int, vars: NodeVars) -> list[str]:
    """`${VAR}` / `${VAR:-default}` / `${VAR:?message}` in field values and
    prose. A heading line is left alone: the shape of the document must read
    the same whether or not you know what a variable holds."""
    out: list[str] = []
    for offset, line in enumerate(lines):
        lineno = first_line + offset
        if HEADING_RE.match(line) or "${" not in line:
            out.append(line)
            continue

        def replace(match: re.Match) -> str:
            name = match.group(1)
            form = match.group(2)
            extra = match.group(3) or ""
            value = vars.get(name)
            if value is not None and (value or form != ":-"):
                return value
            if form == ":-":
                return extra
            if form == ":?":
                raise RunbookError(f"{path}:{lineno}: ${{{name}}} is not set — {extra}")
            raise RunbookError(
                f"{path}:{lineno}: ${{{name}}} is not set — set it with "
                f"`a8s vars <node> set {name} <value>`, or give the reference a "
                f"default (${{{name}:-...}})"
            )

        out.append(VAR_RE.sub(replace, line))
    return out


# --- the extends chain -------------------------------------------------------


def builtin_names() -> list[str]:
    if not BUILTIN_DIR.is_dir():
        return []
    return sorted(p.stem for p in BUILTIN_DIR.glob("*.md"))


def looks_like_path(spec: str) -> bool:
    """Whether a runbook reference names a file rather than a built-in. One
    rule for `extends:` and for `r4t add`, so the two never disagree about
    what `triforce` means."""
    raw = spec.strip()
    return (
        raw.startswith((".", "/", "~"))
        or "/" in raw
        or "\\" in raw
        or raw.endswith(".md")
    )


def _resolve_extends(spec: str, base_dir: Path, path: Path) -> Path:
    raw = spec.strip()
    if looks_like_path(raw):
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        if not candidate.is_file():
            raise RunbookError(
                f"{path}: extends: {spec!r} does not resolve — no file at {candidate}"
            )
        return candidate.resolve()
    candidate = BUILTIN_DIR / f"{raw}.md"
    if not candidate.is_file():
        raise RunbookError(
            f"{path}: extends: {spec!r} names no built-in runbook — "
            f"built-ins are: {', '.join(builtin_names())} "
            f"(a path must start with ./ or ../ or end in .md)"
        )
    return candidate.resolve()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise RunbookError(f"cannot read runbook {path}: {e}") from e


def _load_chain(path: Path, vars: NodeVars) -> list[RunbookFile]:
    """Every file in the `extends:` chain, base FIRST, each interpolated
    before it is merged so the resolved document carries no substitution."""
    chain: list[RunbookFile] = []
    seen: list[Path] = []
    current = path.resolve()
    while True:
        if current in seen:
            loop = " -> ".join(p.name for p in [*seen, current])
            raise RunbookError(f"{path}: extends: forms a cycle ({loop})")
        seen.append(current)
        if len(seen) > MAX_EXTENDS_DEPTH + 1:
            raise RunbookError(
                f"{path}: extends: chain is deeper than {MAX_EXTENDS_DEPTH} "
                f"({' -> '.join(p.name for p in seen)})"
            )
        text = _read(current)
        label = _label_for(current)
        frontmatter, body, first = _split_frontmatter(text, current)
        interpolated = interpolate(body, current, first, vars)
        head = "\n".join(text.splitlines()[: first - 1])
        parsed = parse_file(
            "\n".join([head, *interpolated]) if head else "\n".join(interpolated),
            current,
            label,
        )
        parsed.frontmatter = frontmatter
        chain.append(parsed)
        base = str(frontmatter.get("extends") or "").strip()
        if not base:
            break
        current = _resolve_extends(base, current.parent, current)
    chain.reverse()
    return chain


def _label_for(path: Path) -> str:
    if path.parent == BUILTIN_DIR:
        return path.stem
    return path.name


# --- semantics ---------------------------------------------------------------


def _at(block: Block, key: str, path: Path, message: str) -> str:
    """An error the reader can jump to. Every one names the line, the offending
    token, and the closed set it should have come from."""
    line = block.lines.get(key, block.line)
    return f"{path.name}:{line}: {message}"


def _check_keys(block: Block, allowed: set[str], kind: str, path: Path) -> list[str]:
    errors: list[str] = []
    for key in block.fields:
        if key in allowed:
            continue
        if key in GONE_KEYS:
            errors.append(_at(block, key, path, GONE_KEYS[key]))
        elif key in DEFERRED_KEYS:
            errors.append(_at(block, key, path, DEFERRED_KEYS[key]))
        else:
            errors.append(
                _at(
                    block,
                    key,
                    path,
                    f"unknown {kind} field {key!r} — a {kind} block takes: "
                    f"{', '.join(sorted(allowed))}",
                )
            )
    for key, values in block.fields.items():
        if len(values) > 1 and key != "env":
            errors.append(
                _at(block, key, path, f"{key!r} is set {len(values)} times; only Env: repeats")
            )
    return errors


def _check_name(name: str, kind: str) -> str | None:
    if ":" in name:
        return (
            f"{kind} name {name!r} contains a colon — the colon separates a node "
            f"from a member (`tell node:member`) and cannot appear inside a name"
        )
    if not NAME_RE.fullmatch(name):
        return (
            f"{kind} name {name!r} is not a valid address — letters, digits, "
            f"underscore and hyphen only, starting with a letter or digit"
        )
    return None


def parse_engine_line(value: str) -> dict:
    """`<engine-id> [--model M] [--permissions MODE] [--allowed-tools SPEC]
    [--timeout S]` -> the rig spec it names. The flag set is closed on
    purpose: this is AR3's own translated vocabulary, not an argv
    passthrough into a repo-controlled file."""
    try:
        tokens = shlex.split(value)
    except ValueError as e:
        raise RunbookError(f"Engine: cannot be read as a command line ({e})") from e
    if not tokens:
        raise RunbookError("Engine: names no engine")
    preset = tokens[0].strip().lower()
    if preset not in HARNESS_PRESETS:
        raise RunbookError(
            f"Engine: {tokens[0]!r} is not an engine — choose one of: "
            f"{', '.join(preset_names())}"
        )
    model: str | None = None
    permissions: str | None = None
    allowed_tools: str | None = None
    timeout: str | None = None
    index = 1
    while index < len(tokens):
        flag = tokens[index]
        if flag == "--continue":
            raise RunbookError(
                "Engine: takes no --continue — continuation is per member, not "
                "per rig (try: `- **Continue:** on` on the member)"
            )
        if flag not in ENGINE_FLAGS:
            raise RunbookError(
                f"Engine: unknown flag {flag!r} — the engine line takes "
                f"{', '.join(ENGINE_FLAGS)}"
            )
        if index + 1 >= len(tokens):
            raise RunbookError(f"Engine: {flag} needs a value")
        raw = tokens[index + 1]
        if flag == "--model":
            model = raw
        elif flag == "--permissions":
            if raw not in PERMISSION_MODES:
                raise RunbookError(
                    f"Engine: --permissions {raw!r} is not a stance — one of: "
                    f"{', '.join(PERMISSION_MODES)}"
                )
            permissions = raw
        elif flag == "--allowed-tools":
            allowed_tools = raw
        else:
            timeout = raw
        index += 2

    entry = HARNESS_PRESETS[preset]
    spec: dict = {"preset": preset, "invoke": build_preset_invoke(preset, model=model)}
    if model and entry.get("model_resolver"):
        spec["model"] = model
        spec["model_resolver"] = entry["model_resolver"]
    if permissions:
        spec["permissions"] = permissions
    if allowed_tools:
        spec["allowed_tools"] = allowed_tools
    if timeout:
        try:
            spec["timeout_seconds"] = float(timeout)
        except ValueError:
            raise RunbookError(
                f"Engine: --timeout {timeout!r} is not a number of seconds"
            ) from None
    return spec


def _budget(value: str, label: str) -> tuple[float, float]:
    match = BUDGET_RE.match(value.strip())
    if not match:
        raise RunbookError(
            f"{label} must read like `8 per hour, max 16` (got {value!r})"
        )
    return float(match.group(1)), float(match.group(2))


def _ceiling_problem(
    rig: Rig | None, ceiling: str, node: str | None
) -> str | None:
    """Why this rig asks for more than the machine grants, or None."""
    if rig is None or rig.permissions is None:
        return None
    if permission_rank(rig.permissions) <= permission_rank(ceiling):
        return None
    return ceiling_refusal(rig.permissions, ceiling, node)


def _rig_from_block(
    block: Block, path: Path, ceiling: str, node: str | None
) -> Rig:
    """A `## Rigs` block -> the same Rig a rigs.json entry produces. Both go
    through one validator, so a stance a preset cannot reach fails the rig
    closed here exactly as it does on the machine config."""
    errors = _check_keys(block, RIG_KEYS, "rig", path)
    engine = block.one("engine")
    if not engine:
        errors.append(f"{path.name}:{block.line}: a rig block needs an Engine: line")
        spec: dict = {}
    else:
        try:
            spec = parse_engine_line(engine)
        except RunbookError as e:
            errors.append(_at(block, "engine", path, str(e)))
            spec = {}
    if block.one("allowedtools") and "allowed_tools" in spec:
        errors.append(
            _at(
                block,
                "allowedtools",
                path,
                "the rig sets Allowed tools: and Engine: --allowed-tools — delete one",
            )
        )
    elif block.one("allowedtools"):
        spec["allowed_tools"] = block.one("allowedtools")

    for key, (earn_field, max_field), label in (
        ("rigbudget", ("rig_budget_earn_per_hour", "rig_budget_max"), "Rig budget"),
        ("memberbudget", ("budget_earn_per_hour", "budget_max"), "Member budget"),
    ):
        raw = block.one(key)
        if not raw:
            continue
        try:
            earn, cap = _budget(raw, label)
        except RunbookError as e:
            errors.append(_at(block, key, path, str(e)))
        else:
            spec[earn_field] = earn
            spec[max_field] = cap

    for key, spec_key in (
        ("maxsends", "max_sends_per_turn"),
        ("history", "history_max_bytes"),
        ("echomax", "echo_max_chars"),
    ):
        raw = block.one(key)
        if not raw:
            continue
        try:
            spec[spec_key] = int(float(raw))
        except ValueError:
            errors.append(_at(block, key, path, f"{key} must be a number (got {raw!r})"))

    for key, spec_key, label in (("mcp", "mcp", "MCP"), ("echo", "echo", "Echo")):
        raw = block.one(key)
        if not raw:
            continue
        value, err = parse_bool_field(raw, label)
        if err:
            errors.append(_at(block, key, path, err))
        else:
            spec[spec_key] = value

    env: dict[str, str] = {}
    for entry in block.fields.get("env", []):
        name, sep, value = entry.partition("=")
        if not sep or not name.strip():
            errors.append(
                _at(block, "env", path, f"Env: must read like NAME=value (got {entry!r})")
            )
            continue
        env[name.strip()] = value.strip()
    if env:
        spec["env"] = env

    rig = rig_from_spec(block.name.strip().lower(), spec)
    if rig.error:
        errors.append(f"{path.name}:{block.line}: {rig.error}")
    over = _ceiling_problem(rig, ceiling, node)
    if over:
        errors.append(_at(block, "engine", path, over))
        rig.permissions = None
    if errors:
        rig.error = "; ".join(errors)
    return rig


def _member_from_block(
    block: Block,
    rigs: dict[str, Rig],
    root: Path,
    path: Path,
    ceiling: str,
    node: str | None,
) -> Member:
    errors = _check_keys(block, MEMBER_KEYS, "member", path)
    name_problem = _check_name(block.name, "member")
    fields = {key: block.one(key) for key in block.fields if key not in ("env",)}
    engine = fields.pop("engine", "")
    rig_name = fields.get("rig", "")

    override: Rig | None = None
    if engine and rig_name:
        errors.append(
            _at(
                block,
                "engine",
                path,
                f"{block.name} carries both Engine: and Rig: — delete one. The "
                f"rig is {rig_name!r}; the inline line is {engine!r}",
            )
        )
    elif engine:
        try:
            override = rig_from_spec(block.name.strip().lower(), parse_engine_line(engine))
        except RunbookError as e:
            errors.append(_at(block, "engine", path, str(e)))
        else:
            over = _ceiling_problem(override, ceiling, node)
            if override.error:
                errors.append(_at(block, "engine", path, override.error))
                override = None
            elif over:
                errors.append(_at(block, "engine", path, over))
                override = None
    elif rig_name:
        # The determinism ruling: a rig named in `## Rigs` shadows a machine rig
        # of the same name, whole-block. Unresolved here, the name falls through
        # to the machine config, which fails it closed if nothing answers.
        override = rigs.get(rig_name.strip().lower())
    else:
        errors.append(
            f"{path.name}:{block.line}: {block.name} names neither Engine: nor "
            f"Rig: — there is nothing to run"
        )

    member = member_from_fields(block.name, fields, require_rig=False)
    member.persona = block.text
    member.rig_override = override
    if override is not None:
        member.rig = override.name
    if member.workdir:
        member.workdir = str(_resolve_relative(member.workdir, root))
    if name_problem:
        errors.insert(0, name_problem)
    member.errors = errors + member.errors
    return member


def _resolve_relative(raw: str, root: Path) -> Path:
    """Every relative path in a runbook resolves against the node directory —
    one base, no second rule."""
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (root / p)


def _cell_from_block(block: Block, path: Path) -> Cell:
    errors = _check_keys(block, CELL_KEYS, "cell", path)
    name_problem = _check_name(block.name, "cell")
    if name_problem:
        errors.insert(0, name_problem)
    cell = Cell(name=block.name, lead=block.one("lead"), prose=block.prose)
    ingress = block.one("ingress")
    if ingress:
        cell.ingress, ingress_err = parse_bool_field(ingress, "Ingress")
        if ingress_err:
            errors.append(ingress_err)
    cell.errors = errors
    return cell


def _ritual_from_block(block: Block, path: Path) -> Ritual:
    errors = _check_keys(block, RITUAL_KEYS, "ritual", path)
    ritual = Ritual(
        name=block.name,
        when=block.one("when"),
        to=block.one("to"),
        prompt=block.prose,
    )
    if not ritual.when:
        errors.append("a ritual needs a When: line")
    elif not SCHEDULE_RE.match(ritual.when.strip()):
        errors.append(f"{SCHEDULE_HINT} (got {ritual.when!r})")
    if not ritual.to:
        errors.append("a ritual needs a To: line naming a member or a cell")
    budget = block.one("budget")
    if budget:
        low = budget.strip().lower()
        if low not in ("charge", "free"):
            errors.append(f"Budget must be charge or free (got {budget!r})")
        else:
            ritual.budget = low
    ritual.errors = errors
    return ritual


# --- loading -----------------------------------------------------------------


def runbook_path(root: Path) -> Path:
    return root / RUNBOOK_NAME


def has_runbook(root: Path) -> bool:
    return runbook_path(root).is_file()


def is_runbook(path: Path) -> bool:
    """Whether this file is a runbook rather than a legacy `ROSTER.md`.

    Normally the name answers it. `r4t add` also takes a built-in and an
    explicit path, so a file that is plainly a runbook — frontmatter, or one
    of the six `##` sections — reads as one wherever it sits. Guessing wrong
    would parse a runbook as a roster and report every ritual as a member
    missing a rig, which is why the shape gets a vote and not just the name.
    """
    if path.name == RUNBOOK_NAME or path.parent == BUILTIN_DIR:
        return True
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if text.lstrip().startswith("---"):
        return True
    for line in text.splitlines():
        head = H2_RE.match(line)
        if head and head.group(1).strip().lower() in _SECTION_BY_KEY:
            return True
    return False


def legacy_conflict(root: Path) -> str | None:
    """The warning a node earns by carrying both formats. The runbook wins;
    saying which files are being ignored is the whole point of saying it."""
    if not has_runbook(root):
        return None
    stale = [
        name
        for name in ("ROSTER.md", "MISSION.md", "CHARTER.md", "r4t-org.json")
        if (root / name).is_file()
    ]
    if not stale:
        return None
    return (
        f"{runbook_path(root)} is the roster; {', '.join(stale)} in the same "
        f"directory {'is' if len(stale) == 1 else 'are'} ignored (delete "
        f"{'it' if len(stale) == 1 else 'them'}, or delete the runbook)"
    )


def _merge(chain: list[RunbookFile]) -> tuple[dict, dict[str, Section], str]:
    frontmatter: dict = {}
    sections: dict[str, Section] = {}
    for parsed in chain:
        frontmatter.update(parsed.frontmatter)
        for name, section in parsed.sections.items():
            sections[name] = section
    frontmatter.pop("extends", None)
    return frontmatter, sections, chain[-1].preamble


def _node_root(path: Path, node: str | None) -> Path:
    """The node directory this runbook's relative paths resolve against.

    A registered node's directory is the one r4t recorded for it, which is the
    answer even when the runbook itself lives elsewhere — a built-in named at
    `r4t add`, or a shared file kept out of the repo. Without a registration
    there is nothing better than the file's own directory, which is where a
    runbook normally sits anyway.
    """
    if node:
        import state

        stamped = state.read_root(node)
        if stamped is not None and stamped.is_dir():
            return stamped
    return path.parent


def load_runbook(
    path: Path,
    *,
    node: str | None = None,
    vars: NodeVars | None = None,
    validate: bool = True,
) -> Runbook:
    """Read `path`, resolve its `extends:` chain and its variables, and hand
    back the roster, rigs, cells and rituals it declares.

    `validate=False` is for `r4t runbook check`, which has to be able to load
    the very runbook the operator needs to fix. Document-level problems still
    raise even then — a file whose sections cannot be identified has no
    findings to report.
    """
    if not path.is_file():
        raise RunbookError(f"runbook not found: {path}")
    root = _node_root(path, node)
    resolver = vars if vars is not None else NodeVars(node)
    chain = _load_chain(path, resolver)
    frontmatter, sections, preamble = _merge(chain)

    mission = resolver.get(MISSION_VAR)
    if mission:
        sections["Mission"] = Section(
            name="Mission", text=mission.strip(), source=f"node var {MISSION_VAR}"
        )

    book = Runbook(
        path=path,
        root=root,
        name=str(frontmatter.get("name") or root.name),
        frontmatter=frontmatter,
        sections={name: sections[name] for name in SECTIONS if name in sections},
        preamble=preamble,
        chain=[parsed.label for parsed in chain],
    )

    ceiling = machine_ceiling(node)
    rigs_section = sections.get("Rigs")
    if rigs_section:
        for block in rigs_section.blocks:
            book.rigs[block.name.strip().lower()] = _rig_from_block(
                block, path, ceiling, node
            )

    cells_section = sections.get("Cells")
    if cells_section:
        for block in cells_section.blocks:
            book.cells[block.name.strip().lower()] = _cell_from_block(block, path)

    members: list[Member] = []
    roster_section = sections.get("Roster")
    if roster_section:
        for block in roster_section.blocks:
            members.append(
                _member_from_block(block, book.rigs, root, path, ceiling, node)
            )
    book.roster = Roster(
        path=path,
        members=members,
        cells=[cell.name for cell in book.cells.values()],
    )

    rituals_section = sections.get("Rituals")
    if rituals_section:
        for block in rituals_section.blocks:
            book.rituals[block.name.strip().lower()] = _ritual_from_block(block, path)

    _cross_check(book)
    if validate:
        problem = book.roster.leader_problem()
        if problem is not None:
            raise RunbookError(f"{path}: {problem}")
    return book


def _cross_check(book: Runbook) -> None:
    """The checks a single block cannot make: names that must resolve to some
    other block, and duplicates across the one namespace members, cells and
    the node itself share."""
    roster = book.roster
    assert roster is not None
    member_keys = {m.name.lower() for m in roster.members}
    seen: dict[str, int] = {}
    for m in roster.members:
        seen[m.name.lower()] = seen.get(m.name.lower(), 0) + 1
    for m in roster.members:
        if seen[m.name.lower()] > 1:
            m.errors.append("duplicate roster entry")
        if m.name.lower() in book.cells:
            m.errors.append(
                f"{m.name} names both a member and a cell — `tell node:{m.name}` "
                f"has to mean one thing"
            )
        if m.lead and m.lead.lower() not in member_keys:
            m.errors.append(f"Lead {m.lead!r} is not a member of this roster")
        if m.cell and book.cells and m.cell.lower() not in book.cells:
            m.errors.append(
                f"Cell {m.cell!r} is not declared in `## Cells` "
                f"(declared: {', '.join(sorted(book.cells)) or 'none'})"
            )
    for ritual in book.rituals.values():
        if ritual.to and ritual.to.lower() not in member_keys | set(book.cells):
            ritual.errors.append(
                f"To {ritual.to!r} names neither a member nor a cell"
            )

    used_rigs = {(m.rig or "").lower() for m in roster.members}
    for name in sorted(book.rigs):
        if name not in used_rigs:
            book.warnings.append(f"rig {name!r} is declared and no member names it")
    joined = {m.cell.lower() for m in roster.members if m.cell}
    for name in sorted(book.cells):
        if name not in joined:
            book.warnings.append(f"cell {name!r} is declared and no member joins it")
    for m in roster.members:
        if m.ingress and not m.leader:
            book.warnings.append(
                f"{m.name} has Ingress: on and is not the leader — that is a "
                f"second door into the roster"
            )
    for key in sorted(book.frontmatter):
        if key not in FRONTMATTER_KEYS:
            book.warnings.append(
                f"frontmatter key {key!r} is not recognized — accepted: "
                f"{', '.join(sorted(FRONTMATTER_KEYS))}"
            )
    declared = sorted(r.name for r in book.rituals.values() if not r.errors)
    if declared:
        # Firing is #137; until it lands the declaration says so out loud.
        book.warnings.append(
            "rituals (" + ", ".join(declared) + ") are declared and "
            "validated; this release does not run them — the idle mission "
            "review is built-in behavior, not a ritual block"
        )


def load_for_root(
    root: Path, *, node: str | None = None, validate: bool = True
) -> Runbook:
    return load_runbook(runbook_path(root), node=node, validate=validate)


def _section_text(root: Path, node: str | None, section: str) -> str:
    """One resolved prose section, or "" — including when the runbook will not
    load. Naming a broken runbook is the roster load's job, and it has already
    done it by the time a prompt is being built; repeating it here would put
    the same sentence in front of the reader twice."""
    if not has_runbook(root):
        return ""
    try:
        book = load_for_root(root, node=node, validate=False)
    except RunbookError:
        return ""
    found = book.sections.get(section)
    return found.text.strip() if found else ""


def mission_text(root: Path, node: str | None = None) -> str:
    """The node's mission: the runbook's `## Mission` when it carries one,
    else `MISSION.md`. "" when neither says anything."""
    if has_runbook(root):
        return _section_text(root, node, "Mission")
    try:
        return (root / "MISSION.md").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def mission_source(root: Path) -> Path:
    """The file whose mtime re-arms mission review — whichever one states the
    mission."""
    return runbook_path(root) if has_runbook(root) else root / "MISSION.md"


def charter_text(root: Path, node: str | None = None) -> str:
    """The runbook's `## Charter`. Only a runbook has one, deliberately: there
    is no `CHARTER.md` fallback because nothing ever read such a file."""
    return _section_text(root, node, "Charter")


def org_settings(root: Path) -> dict | None:
    """The frontmatter keys that used to live in `r4t-org.json`, or None when
    this node carries no runbook. Frontmatter is never interpolated, so this
    resolves with no node name and no registry — which is what lets the org
    layer call it without asking the roster who it is first."""
    path = runbook_path(root)
    if not path.is_file():
        return None
    try:
        frontmatter, _body, _first = _split_frontmatter(_read(path), path)
    except RunbookError:
        return None
    out = dict(frontmatter)
    out.pop("extends", None)
    return out


# --- rendering ---------------------------------------------------------------


def render(book: Runbook, *, sources: bool = False) -> str:
    """The merged, interpolated truth, as markdown. With `sources`, every
    heading carries the layer that put it there — the one command that answers
    "why is this value what it is" for a format with inheritance."""
    lines = ["---"]
    for key in sorted(book.frontmatter):
        value = book.frontmatter[key]
        if isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, list):
            inner = ", ".join(f'"{item}"' for item in value)
            lines.append(f"{key}: [{inner}]")
        else:
            lines.append(f'{key}: "{value}"')
    lines.append("---")
    lines.append("")
    if book.preamble:
        lines.append(book.preamble)
        lines.append("")
    for name in SECTIONS:
        section = book.sections.get(name)
        if section is None:
            continue
        heading = f"## {name}"
        if sources:
            heading = f"{heading}{' ' * max(1, 46 - len(heading))}[{section.source}]"
        lines.append(heading)
        lines.append("")
        if section.text:
            lines.append(section.text)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
