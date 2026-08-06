"""Roster parsing — in-repo ROSTER.md describing roster members.

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

AI is the default and carries no marker. The human seat is marked
`- **Human:** yes` and is never dispatched; an optional
`- **Address:** <a8s-name>` tells members how to reach them. A human with
a `Rig:` is an error — humans sit outside the turn system. The Rig
value is a SYMBOLIC rig name resolved against the out-of-repo rig
config — never a command. Parsing is defensive: a malformed block disables
that one member (Member.error set) without crashing dispatch.

An optional `- **Workdir:** <path>` gives the member its own working
directory for turns. Relative paths resolve against the org workplace
(`agents/bob/`, `.bob/`); absolute and `~` paths are allowed and may live
outside the repo entirely. Absent means the member runs from the workplace
root. The directory is created on demand at the start of a turn.
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
    human: bool = False
    rig: str | None = None
    role: str = ""
    leader: bool = False
    address: str | None = None
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
    errors: list[str] = field(default_factory=list)

    @property
    def is_human(self) -> bool:
        return self.human

    @property
    def error(self) -> str | None:
        return "; ".join(self.errors) if self.errors else None


@dataclass
class Roster:
    path: Path
    members: list[Member] = field(default_factory=list)

    def find(self, name: str) -> Member | None:
        key = name.strip().lower()
        for m in self.members:
            if m.name.lower() == key:
                return m
        return None

    def leader(self) -> Member | None:
        for m in self.members:
            if m.leader and not m.is_human:
                return m
        return None

    def names(self) -> list[str]:
        return [m.name for m in self.members]

    @property
    def declares_tree(self) -> bool:
        """True once any AI member carries a `Lead:` line. A roster without
        Lead lines is a flat roster — one cell under the leader — and every
        tree behavior (information hiding, hard rerouting, tree lint) is off."""
        return any(m.lead for m in self.members if not m.is_human)

    def _ai_members(self) -> list[Member]:
        return [m for m in self.members if not m.is_human and not m.errors]

    def reports_to(self, member: Member) -> list[Member]:
        """AI members whose `Lead:` names this member (its direct reports)."""
        key = member.name.lower()
        return [m for m in self._ai_members() if m.lead.lower() == key]

    def adjacent(self, member: Member) -> list[Member]:
        """The members a tree node may reach directly: its lead, its direct
        reports, and its cell-mates — plus every roster human (the seat is
        always visible and reachable). Excludes the member itself and errored
        AI members. Order: lead, reports, remaining cell-mates, humans."""
        picked: dict[str, Member] = {}

        def add(m: Member) -> None:
            if m.name.lower() != member.name.lower():
                picked.setdefault(m.name.lower(), m)

        if member.lead:
            led = self.find(member.lead)
            if led is not None and not led.is_human and not led.errors:
                add(led)
        for m in self.reports_to(member):
            add(m)
        if member.cell:
            for m in self._ai_members():
                if m.cell.lower() == member.cell.lower():
                    add(m)
        for m in self.members:
            if m.is_human:
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


def resolve_roster_path(root: Path, raw: str | None) -> Path:
    if not raw:
        return root / DEFAULT_ROSTER_NAME
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p
    return root / p


def _clean(value: str) -> str:
    return value.strip().strip("`").strip("*").strip()


def _is_true(value: str) -> bool:
    return value.strip().lower() in ("yes", "true", "y", "1", "on")


def _is_false(value: str) -> bool:
    return value.strip().lower() in ("", "no", "false", "n", "off")


def _member_from_block(name: str, lines: list[str]) -> Member:
    m = Member(name=name)
    m.persona = "\n".join([f"### {name}"] + lines).rstrip()
    fields: dict[str, str] = {}
    for line in lines:
        match = FIELD_RE.match(line)
        if not match:
            continue
        key = match.group(1).lower()
        if key not in fields:
            fields[key] = _clean(match.group(2))

    if "status" in fields:
        m.errors.append(
            "Status: is gone — mark the human seat with **Human:** yes; "
            "AI members carry no marker"
        )
    m.human = _is_true(fields.get("human", ""))

    m.role = fields.get("role", fields.get("mandate", ""))
    m.leader = _is_true(fields.get("leader", ""))
    m.address = fields.get("address") or None
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
    if pr and _is_false(pr):
        m.prose_reply = False
    elif pr and not _is_true(pr):
        m.errors.append(
            f"ProseReply must be on or off, got {pr!r} (try: ProseReply: off)"
        )
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

    rig = fields.get("rig", "")
    if rig and m.is_human:
        m.errors.append(
            "Human members carry no Rig — humans are outside the turn system"
        )
    elif rig:
        if RIG_RE.match(rig):
            m.rig = rig.lower()
        else:
            m.errors.append(
                f"Rig must be a symbolic rig name, not a command (got {rig!r})"
            )
    elif not m.is_human:
        m.errors.append("missing Rig line")
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


def load_roster(path: Path) -> Roster:
    if not path.is_file():
        raise RosterError(f"roster not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise RosterError(f"cannot read roster {path}: {e}") from e
    return parse_roster(text, path)
