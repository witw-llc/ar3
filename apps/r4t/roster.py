"""Roster parsing — the members a node dispatches, and the fields they carry.

Two files produce one Roster. `ROSTER.md` is the flat form parsed here; a
`r4t.md` runbook (runbook.py) is the whole team in one file and wins when both
sit at the node dir. Both build their members through `member_from_fields`, so
`Continue:`, `Knowledge:`, `Framing:` and the rest mean exactly one thing.

Format: `### <Name>` blocks with bullet fields:

    ### Phil
    - **Rig:** junior-dev
    - **Role:** Lead Backend Engineer
    - **Leader:** yes
    - **Continue:** on
    Free persona prose lives anywhere in the block.

`Continue:` (default off) runs the member's turns inside its CLI's own
conversation instead of a fresh prompt every wake — the agent keeps its recent
work, and the provider cache makes an expensive rig affordable. It needs a rig
whose preset supports it (rig.py), and members sharing a CLI in one directory
share that conversation, so `r4t roster check` warns about the overlap.

`Continue: on` keeps that conversation until something else retires it.
`Continue: 15m` — bare seconds or a duration with an s/m/h/d suffix — bounds
it: the `r4t idle` sweep retires a conversation that has sat idle that long,
dumping state to disk so the member refounds on the next real message. Any
other value is a member error.

`ProseReply:` (default on) controls whether a clean turn that produces prose
but never addresses anyone with `tell` gets that prose staged as one reply to
the inbound sender. `ProseReply: off` keeps such a member silent — SILENT
logged, nothing staged. Any value other than on/off is a member error.

`Reinforce:` is a short operator-authored line injected into every wake
prompt for this member — founding, continue, echo, batch alike — late in the
prompt where a small model reads it last. It is per-member prompt engineering
distilled from watching that member misbehave ("stay in your lane"
hammering), distinct from the mission (roster-wide intent) and from the
persona (who the member is). The value is kept verbatim; `r4t roster check`
warns past 200 characters, because a paragraph is a mission, not a
reinforcement.

Every member is dispatchable and says what runs it. In `ROSTER.md` that is
always a `Rig:` — a SYMBOLIC rig name resolved against the out-of-repo rig
config, never a command. A runbook member may instead carry an inline
`Engine:` line, which arrives here as a resolved `rig_override`. Parsing is
defensive either way: a malformed block disables that one member
(Member.error set) without crashing dispatch.

The operator is not a member. They speak into the roster with
`r4t tell --as <member>` and read it with `r4t logs` — a roster is a set of
agents that take turns, and someone who never takes one has no row in it.

Exactly one member carries `- **Leader:** yes`, and `load_roster` refuses
a roster that does not have one. The leader is the apex: mail addressed to
the node with nothing past the colon lands on its queue, so a roster with no
leader has no door, and a roster with two has a door the router would have to
guess at. This is the one check that stops at the roster rather than the
member: a malformed block costs its own member, but a roster nobody can
address costs the whole load.

An optional `- **Workdir:** <path>` gives the member its own working
directory for turns. In `ROSTER.md`, relative paths resolve against the org
workplace (`agents/bob/`, `.bob/`); a runbook resolves them against the node
dir instead — one base, no second rule — and hands them here already
absolute. Absolute and `~` paths are allowed and may live outside the repo
entirely. Absent means the member runs from the workplace root. The directory
is created on demand at the start of a turn.
It sets the turn's cwd — which every rig receives, `ollama launch` wrappers
included — fills a `{workdir}` in the rig's invoke for a CLI that takes its
working directory as an argument, and the prompt names it as the member's root.
No harness is obliged to treat it as the project root, though:
opencode-family rigs also advertise the enclosing git root to the model as a
"workspace root". A workdir nested in a repo can therefore still attract writes
to the repo root (see docs/r4t-rigs.md).

An optional `- **Knowledge:** on` (default off) gives the member a private
k7e store (docs/r4t-knowledge.md). The grammar, in ascending specificity:
`on` (defaults), a T-shirt size (`small`/`medium`/`large` — the primary
grammar, mapped to bytes by `KNOWLEDGE_SIZES` below), an exact byte count
(`4k`/`4096` — the escape hatch), a rig name (a distill-rig override at the
default budget), or `<size> <rig>` (both). Sizes are a closed set; any other
single token is taken as a rig name — `parse_knowledge` only checks its
shape, never whether it names a configured rig, because roster parsing has
no rig config to check against; `r4t roster check` and dream-time resolution
own that validation. The inject budget itself is always BYTES, never tokens.

An optional `- **Framing:** ...` (default absent) overrides the cautionary
line under the `## Knowledge` header for this member — `default` (or absent)
keeps the built-in wording, `off` drops the line entirely (the header and
entries still render), and a double-quoted string is custom wording taken
verbatim (docs/r4t-knowledge.md). Quotes are mandatory for custom text:
without them there is no way to tell the keyword `off` apart from an
operator's own sentence that happens to start with the word off. A rig entry
in rigs.json may carry the same three forms as its own default (rig.py); an
explicit member line always wins over the rig's.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rig import Rig

DEFAULT_ROSTER_NAME = "ROSTER.md"

HEADING_RE = re.compile(r"^###\s+(.+?)\s*$")
STOP_RE = re.compile(r"^#{1,3}\s")
FIELD_RE = re.compile(r"^-\s+\*\*([A-Za-z]+):\*\*\s*(.*?)\s*$")
RIG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
FLUSH_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd]?)$", re.IGNORECASE)

FLUSH_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


CONTINUE_ERROR = (
    "Continue must be on, off, or an idle window like 15m "
    "(seconds or a duration with an s/m/h/d suffix)"
)

LEADER_REMEDY = (
    "mark exactly one member with `- **Leader:** yes` — mail addressed to "
    "the node itself lands on the leader"
)


def parse_flush(value: str) -> float:
    match = FLUSH_RE.match(value.strip())
    if not match:
        raise ValueError(f"{CONTINUE_ERROR} (got {value!r})")
    seconds = float(match.group(1)) * FLUSH_UNIT_SECONDS[match.group(2).lower()]
    if seconds <= 0:
        raise ValueError(f"{CONTINUE_ERROR} (got {value!r})")
    return seconds


# T-shirt sizes -> bytes. r4t owns this mapping (not any one rig or preset) so
# a roster written today stays meaningful as the industry's usable context
# grows: move `large` here and every roster using it moves with it, no roster
# edits. `small` is 4096 (k-budget-packing): at the rank-proportional
# packer (knowledge.py), 4096 covers 26/48 LongMemEval questions against 14/48
# for the old 2048 greedy-whole default, without the flat-cap regression that
# dropped single-session-assistant coverage. `medium`/`large` are unmoved by
# that experiment; `large` is currently unreachable in practice because
# SEARCH_LIMIT caps the retrieved pool well under 32768 (tracked separately).
KNOWLEDGE_SIZES: dict[str, int] = {
    "small": 4096,
    "medium": 8192,
    "large": 32768,
}
KNOWLEDGE_DEFAULT_BUDGET = KNOWLEDGE_SIZES["small"]  # global floor for rigs with no tier
KNOWLEDGE_SIZE_RE = re.compile(r"^(\d+)\s*(k|kb)?$", re.IGNORECASE)


@dataclass
class KnowledgeSpec:
    """One member's parsed `Knowledge:` line, or `None` from `parse_knowledge`
    for off. `size_bytes=None` means no explicit size was given — the
    effective inject budget resolves later against the member's rig's
    knowledge tier (rig.py), which needs config this module never loads.
    `distill_rig=None` means dreaming uses the member's own turn rig."""

    size_bytes: int | None = None
    distill_rig: str | None = None


def _knowledge_size_bytes(token: str) -> int | None:
    """`token`'s byte value if it is size-shaped (a T-shirt word or a bare
    byte count like `4k`/`4096`), else None — meaning it must be a rig name."""
    low = token.lower()
    if low in KNOWLEDGE_SIZES:
        return KNOWLEDGE_SIZES[low]
    match = KNOWLEDGE_SIZE_RE.match(token)
    if not match:
        return None
    n = int(match.group(1))
    return n * 1024 if match.group(2) else n


_KNOWLEDGE_GRAMMAR_HINT = (
    "Knowledge must be on, off, a T-shirt size (small/medium/large), an "
    "exact byte count like 4k/4096, a rig name, or `<size> <rig>`"
)


def parse_knowledge(value: str) -> KnowledgeSpec | None:
    """`Knowledge:` value -> a KnowledgeSpec, or None for off.

    Sizes are the closed set (T-shirts + numeric); any other single token is
    read as a rig name — its SHAPE is checked here (`RIG_RE`), never whether
    it names a configured rig (see the module docstring)."""
    v = value.strip()
    low = v.lower()
    if low in ("", "off", "no", "false"):
        return None
    if low in ("on", "yes", "true"):
        return KnowledgeSpec()
    tokens = v.split()
    if len(tokens) > 2:
        raise ValueError(f"{_KNOWLEDGE_GRAMMAR_HINT} (got {value!r})")
    size_bytes: int | None = None
    rig_name: str | None = None
    for tok in tokens:
        tok_bytes = _knowledge_size_bytes(tok)
        if tok_bytes is not None:
            if size_bytes is not None:
                raise ValueError(f"Knowledge names two sizes (got {value!r})")
            size_bytes = tok_bytes
        elif RIG_RE.match(tok):
            if rig_name is not None:
                raise ValueError(f"Knowledge names two rigs (got {value!r})")
            rig_name = tok.lower()
        else:
            raise ValueError(f"{_KNOWLEDGE_GRAMMAR_HINT} (got {value!r})")
    return KnowledgeSpec(size_bytes=size_bytes, distill_rig=rig_name)


@dataclass
class FramingSpec:
    """One resolved `Framing:` choice — a member's roster line or a rig's
    config default. `off` drops the framing line entirely under the
    `## Knowledge` header (the header and entries still render); `text=None`
    with `off=False` picks the built-in line; `text` set is custom wording
    taken verbatim."""

    off: bool = False
    text: str | None = None


_FRAMING_GRAMMAR_HINT = (
    "Framing must be default, off, or a double-quoted custom string"
)


def parse_framing(value: str, *, quoted: bool = True) -> FramingSpec:
    """`Framing:` value -> a FramingSpec. `default` (or empty) is the
    built-in framing line, `off` drops it. Custom text: a roster line
    requires double quotes so the `off`/`default` keywords stay
    distinguishable from an operator's own sentence (`quoted=True`, the
    default); a rigs.json value is already a JSON string with no such
    ambiguity, so `quoted=False` (rig.py) accepts any other text verbatim."""
    v = value.strip()
    low = v.lower()
    if low in ("", "default"):
        return FramingSpec()
    if low == "off":
        return FramingSpec(off=True)
    if not quoted:
        return FramingSpec(text=v)
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return FramingSpec(text=v[1:-1])
    raise ValueError(f"{_FRAMING_GRAMMAR_HINT} (got {value!r})")


class RosterError(Exception):
    pass


@dataclass
class Member:
    name: str
    rig: str | None = None
    role: str = ""
    leader: bool = False
    continue_conversation: bool = False
    flush_seconds: float | None = None
    prose_reply: bool = True
    reinforce: str = ""
    knowledge_on: bool = False
    knowledge_bytes: int | None = None
    knowledge_distill_rig: str | None = None
    framing: FramingSpec | None = None
    cell: str = ""
    lead: str = ""
    workdir: str = ""
    persona: str = ""
    # Whether `tell <node>:<member>` from outside the roster delivers to this
    # member. Off by default, and on by default for the leader — the node's
    # door, which bare mail already reaches.
    ingress: bool = False
    # A rig the roster itself declared — a runbook's inline `Engine:` line, or
    # a `## Rigs` block shadowing the machine config. None means `rig` is a
    # symbolic name the machine config resolves, which is the only shape
    # ROSTER.md can produce.
    rig_override: "Rig | None" = None
    errors: list[str] = field(default_factory=list)

    @property
    def error(self) -> str | None:
        return "; ".join(self.errors) if self.errors else None


@dataclass
class Roster:
    path: Path
    members: list[Member] = field(default_factory=list)
    # Cell names, in declaration order. Members and cells share one name space
    # inside a node, so the router needs both lists to tell `node:eng` naming a
    # cell apart from `node:eng` naming nothing at all. A ROSTER.md declares
    # none; only a runbook's `## Cells` fills this in.
    cells: list[str] = field(default_factory=list)

    def find(self, name: str) -> Member | None:
        key = name.strip().lower()
        for m in self.members:
            if m.name.lower() == key:
                return m
        return None

    def leader(self) -> Member | None:
        for m in self.members:
            if m.leader:
                return m
        return None

    def leader_problem(self) -> str | None:
        """Why this roster has no single addressable apex, or None when it
        has one. `load_roster` turns a non-None answer into a RosterError;
        `r4t roster check` prints it instead, because the tool that diagnoses
        a broken roster has to be able to read one."""
        marked = [m for m in self.members if m.leader]
        if len(marked) > 1:
            names = ", ".join(m.name for m in marked)
            return f"marks {len(marked)} leaders ({names}) — {LEADER_REMEDY}"
        if not marked:
            return f"marks no leader — {LEADER_REMEDY}"
        return None

    def names(self) -> list[str]:
        return [m.name for m in self.members]

    @property
    def declares_tree(self) -> bool:
        """True once any member carries a `Lead:` line. A roster without
        Lead lines is a flat roster — one cell under the leader — and every
        tree behavior (information hiding, hard rerouting, tree lint) is off."""
        return any(m.lead for m in self.members)

    def _ai_members(self) -> list[Member]:
        return [m for m in self.members if not m.errors]

    def reports_to(self, member: Member) -> list[Member]:
        """Members whose `Lead:` names this member (its direct reports)."""
        key = member.name.lower()
        return [m for m in self._ai_members() if m.lead.lower() == key]

    def adjacent(self, member: Member) -> list[Member]:
        """The members a tree node may reach directly: its lead, its direct
        reports, and its cell-mates. Excludes the member itself and errored
        members. Order: lead, reports, remaining cell-mates."""
        picked: dict[str, Member] = {}

        def add(m: Member) -> None:
            if m.name.lower() != member.name.lower():
                picked.setdefault(m.name.lower(), m)

        if member.lead:
            led = self.find(member.lead)
            if led is not None and not led.errors:
                add(led)
        for m in self.reports_to(member):
            add(m)
        if member.cell:
            for m in self._ai_members():
                if m.cell.lower() == member.cell.lower():
                    add(m)
        return list(picked.values())

    def _max_tree_depth(self) -> int:
        """Deepest Lead chain measured in hops below the top lead (the AI
        member marked Leader). The top lead is depth 0; a member reporting to
        it is depth 1. Cycles and members that never reach the top are skipped
        rather than counted."""
        top = self.leader()
        if top is None:
            return 0
        top_key = top.name.lower()
        by_name = {m.name.lower(): m for m in self._ai_members()}
        best = 0
        for m in self._ai_members():
            depth = 0
            seen: set[str] = set()
            cur: Member | None = m
            while cur is not None and cur.name.lower() != top_key:
                if cur.name.lower() in seen or not cur.lead:
                    depth = 0  # broken chain — not a real path to the top
                    break
                seen.add(cur.name.lower())
                cur = by_name.get(cur.lead.lower())
                depth += 1
            if cur is not None and cur.name.lower() == top_key:
                best = max(best, depth)
        return best

    def tree_problems(self) -> list[tuple[str, str]]:
        """Lint the declared tree, returning (severity, message) pairs where
        severity is "error" or "warn". Empty for flat rosters (no Lead lines):
        those keep working exactly as before, no new warnings. Checks: a Lead
        must name a roster member; a cell over 6 AI members warns and over 10
        errors (the ORG-LESSONS span-of-control numbers); a tree deeper than 2
        levels below the top lead warns."""
        if not self.declares_tree:
            return []
        out: list[tuple[str, str]] = []
        ai = self._ai_members()
        member_names = {m.name.lower() for m in self.members}
        for m in ai:
            if m.lead and m.lead.lower() not in member_names:
                out.append(("error", f"{m.name}: Lead {m.lead!r} is not a roster member"))
        cells: dict[str, list[Member]] = {}
        for m in ai:
            if m.cell:
                cells.setdefault(m.cell.lower(), []).append(m)
        for cell, mem in sorted(cells.items()):
            n = len(mem)
            if n > 10:
                out.append(("error", f"cell {cell!r} has {n} AI members (hard cap 10)"))
            elif n > 6:
                out.append(("warn", f"cell {cell!r} has {n} AI members (soft cap 6)"))
        depth = self._max_tree_depth()
        if depth > 2:
            out.append((
                "warn",
                f"tree depth {depth} exceeds 2 levels below the top lead",
            ))
        return out


def resolve_roster_path(root: Path, raw: str | None, node: str | None = None) -> Path:
    """Where this node's roster lives, in one order and no second one:

    1. an explicit `--roster`
    2. `r4t.md` at the node dir — the checked-in runbook always wins, which is
       the whole reproducibility ruling: a repo that states how it runs runs
       that way
    3. the runbook `r4t add` named for this node — a built-in, or a path
       elsewhere on disk, recorded out of the repo
    4. `ROSTER.md`

    A legacy roster beside a runbook is named as ignored rather than quietly
    obeyed (`runbook.legacy_conflict`).
    """
    if not raw:
        from runbook import RUNBOOK_NAME

        book = root / RUNBOOK_NAME
        if book.is_file():
            return book
        if node:
            from state import read_runbook

            recorded = read_runbook(node)
            if recorded is not None and recorded.is_file():
                return recorded
        return root / DEFAULT_ROSTER_NAME
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p
    return root / p


def clean_field(value: str) -> str:
    """A field value with the markdown a person types around it removed —
    backticks and emphasis. Shared with the runbook so `Rig: `big`` and
    `Rig: big` are one value in both formats."""
    return value.strip().strip("`").strip("*").strip()


def _is_true(value: str) -> bool:
    return value.strip().lower() in ("yes", "true", "y", "1", "on")


def _is_false(value: str) -> bool:
    return value.strip().lower() in ("", "no", "false", "n", "off")


# The one boolean vocabulary for every runbook/roster boolean field: member
# `Leader:`/`Ingress:`/`ProseReply:`, cell `Ingress:`, rig `MCP:`/`Echo:`.
# `Continue:` is NOT a member of this set — it is time-valued (`on` is a
# synonym for "no idle bound", but a duration like `15m` is a legal value too)
# and keeps its own parsing below, unaffected by this vocabulary.
BOOL_WORDS = "yes/no/true/false/y/n/1/0/on/off"
_BOOL_TRUE_WORDS = ("yes", "true", "y", "1", "on")
_BOOL_FALSE_WORDS = ("no", "false", "n", "0", "off")


def parse_bool_field(raw: str, field: str) -> tuple[bool, str | None]:
    """Parse one loose boolean field. Unset (empty string) reads as False
    with no error. A value that IS present and outside the accepted set is a
    field-level error naming the field, the value, and the set — the same
    loud style every other field error uses — rather than silently reading as
    False, which is how `Leader: maybe` used to become "marks no leader"
    somewhere else entirely, with no line to point at."""
    value = raw.strip().lower()
    if not value:
        return False, None
    if value in _BOOL_TRUE_WORDS:
        return True, None
    if value in _BOOL_FALSE_WORDS:
        return False, None
    return False, f"{field} must be {BOOL_WORDS} (got {raw!r})"


def member_from_fields(
    name: str, fields: dict[str, str], *, require_rig: bool = True
) -> Member:
    """One member from its already-collected fields, shared by ROSTER.md and
    the runbook so the two formats can never drift on what `Continue:` or
    `Knowledge:` mean. Keys are lowercase and space-free.

    `require_rig=False` is the runbook, where a member may instead carry an
    inline `Engine:` line — the presence check moves to the caller, which is
    the layer that knows both spellings.
    """
    m = Member(name=name)
    if "status" in fields:
        m.errors.append("Status: is gone — members carry no marker")
    if "human" in fields:
        m.errors.append(
            "Human: is gone — a roster is members that take turns. "
            "Speak into it with `r4t tell --as <member>`"
        )
    if "address" in fields:
        m.errors.append(
            "Address: is gone with the seat doorbell — mail crossing the "
            "wall is a8s's job, not a member field"
        )

    m.role = fields.get("role", fields.get("mandate", ""))
    m.leader, leader_err = parse_bool_field(fields.get("leader", ""), "Leader")
    if leader_err:
        m.errors.append(leader_err)
    if "flush" in fields:
        m.errors.append(
            "Flush: is not a field — the idle window rides Continue: "
            "(try: Continue: 15m)"
        )
    cont = fields.get("continue", "")
    if _is_true(cont):
        m.continue_conversation = True
    elif not _is_false(cont):
        try:
            m.flush_seconds = parse_flush(cont)
        except ValueError as e:
            m.errors.append(str(e))
        else:
            m.continue_conversation = True
    if "fallback" in fields:
        m.errors.append(
            "Fallback: is gone — the knob is now ProseReply: "
            "(try: ProseReply: off)"
        )
    pr = fields.get("prosereply", "")
    if pr:
        pr_value, pr_err = parse_bool_field(pr, "ProseReply")
        if pr_err:
            m.errors.append(f"{pr_err} (try: ProseReply: off)")
        else:
            m.prose_reply = pr_value
    m.reinforce = fields.get("reinforce", "")
    kn = fields.get("knowledge", "")
    if kn:
        try:
            spec = parse_knowledge(kn)
        except ValueError as e:
            m.errors.append(str(e))
        else:
            if spec is not None:
                m.knowledge_on = True
                m.knowledge_bytes = spec.size_bytes
                m.knowledge_distill_rig = spec.distill_rig
    fr = fields.get("framing", "")
    if fr:
        try:
            m.framing = parse_framing(fr)
        except ValueError as e:
            m.errors.append(str(e))
    m.cell = fields.get("cell", "")
    m.lead = fields.get("lead", "")
    m.workdir = fields.get("workdir", "")

    ing = fields.get("ingress", "")
    if ing:
        ing_value, ing_err = parse_bool_field(ing, "Ingress")
        if ing_err:
            m.errors.append(ing_err)
        else:
            m.ingress = ing_value
    else:
        # The leader's default is on: bare mail to the node already lands
        # there, so `node:leader` naming the same mailbox costs nothing.
        m.ingress = m.leader

    rig = fields.get("rig", "")
    if rig:
        if RIG_RE.match(rig):
            m.rig = rig.lower()
        else:
            m.errors.append(
                f"Rig must be a symbolic rig name, not a command (got {rig!r})"
            )
    elif require_rig:
        m.errors.append("missing Rig line")
    return m


def _member_from_block(name: str, lines: list[str]) -> Member:
    fields: dict[str, str] = {}
    for line in lines:
        match = FIELD_RE.match(line)
        if not match:
            continue
        key = match.group(1).lower()
        if key not in fields:
            fields[key] = clean_field(match.group(2))
    m = member_from_fields(name, fields)
    m.persona = "\n".join([f"### {name}"] + lines).rstrip()
    return m


def parse_roster(text: str, path: Path) -> Roster:
    members: list[Member] = []
    current_name: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_name, current_lines
        if current_name is not None:
            members.append(_member_from_block(current_name, current_lines))
        current_name = None
        current_lines = []

    for line in text.splitlines():
        head = HEADING_RE.match(line)
        if head:
            flush()
            current_name = head.group(1)
            continue
        if STOP_RE.match(line):
            flush()
            continue
        if current_name is not None:
            current_lines.append(line)
    flush()

    by_key: dict[str, list[Member]] = {}
    for m in members:
        by_key.setdefault(m.name.lower(), []).append(m)
    for dupes in by_key.values():
        if len(dupes) > 1:
            for m in dupes:
                m.errors.append("duplicate roster entry")

    return Roster(path=path, members=members)


def load_roster(path: Path, *, validate: bool = True, node: str | None = None) -> Roster:
    """Parse the roster and refuse one with no single leader.

    `validate=False` is for `r4t roster check`, which must load exactly the
    roster the operator has to fix.

    A runbook (`runbook.is_runbook`) goes through the runbook loader and comes
    back as the same Roster every caller here already handles, members
    carrying resolved rigs instead of bare names. `node` names the a8s node
    whose vars the runbook's `${VAR}` references resolve against; without one,
    only `A8S_VAR_*` in the environment answers.
    """
    from runbook import RunbookError, is_runbook, load_runbook

    if is_runbook(path):
        try:
            return load_runbook(path, node=node, validate=validate).roster
        except RunbookError as e:
            raise RosterError(str(e)) from e
    if not path.is_file():
        raise RosterError(f"roster not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise RosterError(f"cannot read roster {path}: {e}") from e
    roster = parse_roster(text, path)
    if validate:
        problem = roster.leader_problem()
        if problem is not None:
            raise RosterError(f"{path}: {problem}")
    return roster
