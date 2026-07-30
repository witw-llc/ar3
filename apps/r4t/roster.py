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

`Fallback:` (default on) controls the stdout-reply fallback in dispatch: a
clean turn that releases nothing normally gets its cleaned stdout staged as
one reply to the inbound sender. `Fallback: off` keeps such a member silent —
SILENT logged, nothing staged. Any value other than on/off is a member error.

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
to the repo root (issue #273; see docs/r4t-rigs.md).
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


KNOWLEDGE_DEFAULT_BUDGET = 2048  # bytes — the field survey's floor; K0 argues it up
KNOWLEDGE_RE = re.compile(r"^(\d+)\s*(k|kb)?$", re.IGNORECASE)


def parse_knowledge(value: str) -> int:
    """`Knowledge:` value -> inject budget in bytes. `on` takes the default
    budget, `off` is 0, a bare size (`4k`, `4096`) sets it exactly."""
    v = value.strip().lower()
    if v in ("", "off", "no", "false"):
        return 0
    if v in ("on", "yes", "true"):
        return KNOWLEDGE_DEFAULT_BUDGET
    match = KNOWLEDGE_RE.match(v)
    if not match:
        raise ValueError(
            f"Knowledge must be on, off, or a size like 4k (got {value!r})"
        )
    n = int(match.group(1))
    return n * 1024 if match.group(2) else n


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
    fallback: bool = True
    reinforce: str = ""
    knowledge_bytes: int = 0
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
    fb = fields.get("fallback", "")
    if fb and _is_false(fb):
        m.fallback = False
    elif fb and not _is_true(fb):
        m.errors.append(
            f"Fallback must be on or off, got {fb!r} (try: Fallback: off)"
        )
    m.reinforce = fields.get("reinforce", "")
    kn = fields.get("knowledge", "")
    if kn:
        try:
            m.knowledge_bytes = parse_knowledge(kn)
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
