"""Task trace — reconstruct one thread's delegation tree from state on disk.

Read-only, and writes nothing of its own: every fact here is already recorded,
just spread across four files. The day log (`log/<date>.md`) is the spine —
append-only, never pruned, and every delivery, turn boundary and closure lands
in it carrying the thread id. The ledger (`tasks/<id>.json`) adds the
originator and the closure stamp while it is live; `expire_tasks` deletes it
once a thread goes idle, so an old thread's trace reconstructs both from the
log instead. The dead-letter dir, the members' queues and any in-flight
`.turn.json` fill in what never got delivered and what is still moving.

velocity.csv is deliberately not read: a row records only a turn's NEWEST
thread, so it cannot answer whether a turn touched this one. The log's dispatch
header lists every thread in the batch, which is the question a trace asks.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field

import state
import tasks

MARKS = {True: "✓", False: "✗"}

QUEUED_RE = re.compile(
    r'^r4t: QUEUED (?P<sender>\S+) -> (?P<to>\S+) thread=(?P<thread>\S+) '
    r'hop=(?P<hop>\d+) "(?P<preview>.*)" \(depth \d+\)$'
)
RELEASED_RE = re.compile(
    r"^r4t: RELEASED(?P<internal>-internal)? (?P<sender>\S+) -> (?P<to>\S+) "
    r"thread=(?P<thread>\S+) hop=(?P<hop>\d+)$"
)
ANSWERED_RE = re.compile(
    r"^r4t: ANSWERED thread=(?P<thread>\S+) (?P<sender>\S+) -> (?P<to>\S+) "
)
DISPATCH_RE = re.compile(
    r"^## (?P<at>\S+) dispatch (?P<messages>\d+) message\(s\) -> (?P<member>\S+) "
    r"\(threads (?P<threads>.*?), rig (?P<rig>[^\s)]+)"
)
OUTPUT_RE = re.compile(
    r"^### Output \((?P<member>[^,]+), exit (?P<exit>-?\d+) in "
    r"(?P<duration>[\d.]+)s(?P<killed> \(killed at timeout [^)]*\))?\)$"
)
EVENT_RE = re.compile(r"^r4t: (?P<kind>[A-Z][A-Z0-9-]*)\b")


@dataclass
class Edge:
    """One delivery attributed to this thread: who sent, who received, at
    which hop."""

    seq: int
    sender: str
    to: str
    hop: int
    preview: str
    external: bool
    closes: bool = False


@dataclass
class Turn:
    at: str
    member: str
    rig: str
    messages: int
    exit_code: int | None = None
    duration: float | None = None
    killed: bool = False


@dataclass
class Trace:
    node: str
    thread: str
    ledger: dict | None
    edges: list[Edge] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    dead_letters: list[dict] = field(default_factory=list)
    queued: list[dict] = field(default_factory=list)
    running: list[dict] = field(default_factory=list)
    answered_by: str | None = None

    @property
    def empty(self) -> bool:
        return not (
            self.ledger
            or self.edges
            or self.turns
            or self.events
            or self.dead_letters
            or self.queued
            or self.running
        )

    @property
    def closed(self) -> bool:
        if self.ledger is not None:
            return self.ledger.get("status") == tasks.STATUS_CLOSED
        return self.answered_by is not None


def _norm(node: str, addr: str) -> str:
    """An address as the trace names it: intra-roster `acme:phil` reads `phil`,
    everything else (outside agents, the `r4t:<node>` dispatcher voice) stays
    verbatim."""
    a = (addr or "").strip().lower()
    prefix = node.lower() + ":"
    return a[len(prefix):] if a.startswith(prefix) else a


def _scan_log(node: str, thread: str) -> tuple[list[Edge], list[Turn], list[str], str | None]:
    edges: list[Edge] = []
    turns: list[Turn] = []
    events: list[str] = []
    answered_by: str | None = None
    # An intra-roster send logs BOTH the recipient's QUEUED and the sender's
    # RELEASED-internal; a send to a human seat logs only the latter, and an
    # external one only RELEASED. Counting QUEUEDs per sender+recipient+hop lets
    # each RELEASED spend one — so every delivery becomes exactly one edge.
    delivered: dict[tuple[str, str, int], int] = {}
    turn: Turn | None = None

    log_dir = state.roster_dir(node) / "log"
    for path in sorted(log_dir.glob("*.md")) if log_dir.is_dir() else []:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            head = DISPATCH_RE.match(line)
            if head:
                batch = [t.strip() for t in head["threads"].split(",")]
                turn = None
                if thread in batch:
                    turn = Turn(
                        at=head["at"],
                        member=head["member"].strip().lower(),
                        rig=head["rig"],
                        messages=int(head["messages"]),
                    )
                    turns.append(turn)
                continue
            done = OUTPUT_RE.match(line)
            if done is not None and turn is not None:
                if done["member"].strip().lower() == turn.member:
                    turn.exit_code = int(done["exit"])
                    turn.duration = float(done["duration"])
                    turn.killed = bool(done["killed"])
                    turn = None
                continue

            queued = QUEUED_RE.match(line)
            if queued is not None:
                if queued["thread"] == thread:
                    key = (
                        _norm(node, queued["sender"]),
                        _norm(node, queued["to"]),
                        int(queued["hop"]),
                    )
                    delivered[key] = delivered.get(key, 0) + 1
                    edges.append(
                        Edge(
                            seq=len(edges),
                            sender=key[0],
                            to=key[1],
                            hop=key[2],
                            preview=queued["preview"],
                            external=False,
                        )
                    )
                continue
            released = RELEASED_RE.match(line)
            if released is not None:
                if released["thread"] == thread:
                    key = (
                        _norm(node, released["sender"]),
                        _norm(node, released["to"]),
                        int(released["hop"]),
                    )
                    if delivered.get(key):
                        delivered[key] -= 1
                    else:
                        edges.append(
                            Edge(
                                seq=len(edges),
                                sender=key[0],
                                to=key[1],
                                hop=key[2],
                                preview="",
                                external=not released["internal"],
                            )
                        )
                continue
            answered = ANSWERED_RE.match(line)
            if answered is not None:
                if answered["thread"] == thread:
                    sender = _norm(node, answered["sender"])
                    to = _norm(node, answered["to"])
                    answered_by = f"{sender} -> {to}"
                    for edge in reversed(edges):
                        if edge.sender == sender and edge.to == to:
                            edge.closes = True
                            break
                continue
            event = EVENT_RE.match(line)
            if event is not None and f"thread={thread}" in line:
                events.append(line[len("r4t: "):])
    return edges, turns, events, answered_by


def _member_names(node: str) -> list[str]:
    root = state.roster_dir(node) / "agents"
    if not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir() if entry.is_dir())


def _in_flight(node: str, thread: str) -> tuple[list[dict], list[dict]]:
    queued: list[dict] = []
    running: list[dict] = []
    locked = {str(lock.get("agent", "")) for lock in state.live_locks(node, prune=False)}
    for member in _member_names(node):
        for envelope in state.read_queue(node, member):
            if str(envelope.get("thread", "")) != thread:
                continue
            queued.append(
                {
                    "member": member,
                    "from": _norm(node, str(envelope.get("from", "?"))),
                    "hop": int(envelope.get("hop", 0) or 0),
                    "preview": " ".join(str(envelope.get("body", "")).split())[:80],
                }
            )
        turn = state.read_turn(node, member)
        if turn is not None and thread in [str(t) for t in turn.get("threads", [])]:
            running.append(
                {
                    "member": member,
                    "started": str(turn.get("started", "?")),
                    "rig": str(turn.get("rig", "?")),
                    "live": member in locked,
                }
            )
    return queued, running


def build(node: str, thread: str) -> Trace:
    edges, turns, events, answered_by = _scan_log(node, thread)
    queued, running = _in_flight(node, thread)
    return Trace(
        node=node,
        thread=thread,
        ledger=tasks.load_task(node, thread),
        edges=edges,
        turns=turns,
        events=events,
        dead_letters=[
            d for d in state.list_dead_letters(node)
            if str(d.get("thread", "")) == thread
        ],
        queued=queued,
        running=running,
        answered_by=answered_by,
    )


def delegation(edges: list[Edge]) -> list[tuple[int, Edge]]:
    """(depth, edge) pairs in delegation order. An edge's parent is the most
    recent earlier delivery INTO its sender — which is what makes a flat
    ordered edge list a tree: the message a member is answering, or the task it
    is passing on. A parent index is always lower than its child's, so the walk
    can never cycle."""
    children: dict[int | None, list[int]] = {}
    latest_into: dict[str, int] = {}
    for i, edge in enumerate(edges):
        children.setdefault(latest_into.get(edge.sender), []).append(i)
        latest_into[edge.to] = i
    ordered: list[tuple[int, Edge]] = []
    stack = [(index, 0) for index in reversed(children.get(None, []))]
    while stack:
        index, depth = stack.pop()
        ordered.append((depth, edges[index]))
        stack.extend(
            (child, depth + 1) for child in reversed(children.get(index, []))
        )
    return ordered


def _rows(rows: list[tuple[bool | None, str, str, str | None]]) -> list[str]:
    if not rows:
        return ["  (none)"]
    width = max(len(name) for _m, name, _t, _h in rows)
    out = []
    for mark, name, text, hint in rows:
        line = f"  {MARKS.get(mark, ' ')} {name:<{width}}  {text}"
        if hint:
            line += f"   (try: {hint})"
        out.append(line.rstrip())
    return out


def _originator(t: Trace) -> str:
    if t.ledger is not None:
        return _norm(t.node, str(t.ledger.get("creator", "?")))
    return t.edges[0].sender if t.edges else "(unknown)"


def _thread_rows(t: Trace) -> list[tuple[bool | None, str, str, str | None]]:
    rows: list[tuple[bool | None, str, str, str | None]] = []
    if t.closed:
        answered = f" (answered by {t.answered_by})" if t.answered_by else ""
        rows.append((True, "status", f"closed{answered}", None))
    else:
        rows.append((None, "status", "open — the originator has not heard back", None))
    rows.append((None, "originator", _originator(t), None))
    if t.ledger is not None:
        rows.append((
            None, "ledger",
            f"opened {t.ledger.get('created_at', '?')}, "
            f"last write {t.ledger.get('updated_at', '?')}",
            None,
        ))
    else:
        rows.append((
            None, "ledger",
            "expired — this trace is reconstructed from the day log",
            None,
        ))
    rows.append((
        None, "volume",
        f"{len(t.turns)} turn(s), {len(t.edges)} delivery(s), "
        f"{len(t.dead_letters)} dead letter(s)",
        None,
    ))
    return rows


def _delegation_lines(t: Trace) -> list[str]:
    ordered = delegation(t.edges)
    if not ordered:
        return ["  (nothing delivered — see In flight or Dead letters below)"]
    labels = [
        f"{'  ' * depth}{edge.sender} -> {edge.to}" for depth, edge in ordered
    ]
    width = max(len(label) for label in labels)
    out = []
    for label, (_depth, edge) in zip(labels, ordered):
        line = f"  {label:<{width}}  hop {edge.hop}"
        if edge.external:
            line += "  (out of the walls)"
        if edge.closes:
            line += "  (closes the thread)"
        if edge.preview:
            line += f'  "{edge.preview}"'
        out.append(line)
    return out


def _turn_rows(t: Trace) -> list[tuple[bool | None, str, str, str | None]]:
    rows: list[tuple[bool | None, str, str, str | None]] = []
    width = max((len(turn.rig) for turn in t.turns), default=0)
    for turn in t.turns:
        if turn.exit_code is None:
            rows.append((None, turn.member, f"rig {turn.rig}  no outcome recorded", None))
            continue
        detail = (
            f"rig {turn.rig:<{width}}  exit {turn.exit_code}  "
            f"{turn.duration:>6.1f}s  {turn.messages} msg  {turn.at}"
        )
        if turn.killed:
            detail += "  (killed at the rig timeout)"
        ok = turn.exit_code == 0 and not turn.killed
        hint = None if ok else f"r4t logs --node {t.node} --agent {turn.member} --full"
        rows.append((ok, turn.member, detail, hint))
    return rows


def render(t: Trace) -> list[str]:
    out = [
        f"task: {t.thread}",
        f"roster: {t.node}  (state: {state.roster_dir(t.node)})",
        "",
        "Thread",
        *_rows(_thread_rows(t)),
        "",
        "Delegation",
        *_delegation_lines(t),
        "",
        "Turns",
        *_rows(_turn_rows(t)),
    ]
    if t.dead_letters:
        out += ["", "Dead letters", *_rows([
            (
                False,
                str(d.get("reason", "?")),
                f"{_norm(t.node, str(d.get('from', '?')))} -> "
                f"{_norm(t.node, str(d.get('to', '?')))}  {d.get('time', '?')}",
                None,
            )
            for d in t.dead_letters
        ])]
    if t.events:
        out += ["", "Events", *[f"    {line}" for line in t.events]]
    if t.running or t.queued:
        rows: list[tuple[bool | None, str, str, str | None]] = [
            (
                run["live"],
                run["member"],
                f"turn running since {run['started']} (rig {run['rig']})"
                + ("" if run["live"] else " — no live lock, so the turn crashed"),
                None,
            )
            for run in t.running
        ] + [
            (
                None,
                q["member"],
                f"queued from {q['from']} at hop {q['hop']}"
                + (f'  "{q["preview"]}"' if q["preview"] else ""),
                None,
            )
            for q in t.queued
        ]
        out += ["", "In flight", *_rows(rows)]
    return out


def payload(t: Trace) -> dict:
    return {
        "node": t.node,
        "thread": t.thread,
        "closed": t.closed,
        "answered_by": t.answered_by,
        "originator": _originator(t),
        "ledger": t.ledger,
        "delegation": [
            {
                "depth": depth,
                "seq": edge.seq,
                "from": edge.sender,
                "to": edge.to,
                "hop": edge.hop,
                "external": edge.external,
                "closes": edge.closes,
                "preview": edge.preview,
            }
            for depth, edge in delegation(t.edges)
        ],
        "turns": [
            {
                "at": turn.at,
                "member": turn.member,
                "rig": turn.rig,
                "messages": turn.messages,
                "exit": turn.exit_code,
                "duration_seconds": turn.duration,
                "timed_out": turn.killed,
            }
            for turn in t.turns
        ],
        "events": t.events,
        "dead_letters": t.dead_letters,
        "queued": t.queued,
        "running": t.running,
    }


def run(node: str, thread_id: str, *, json_mode: bool = False) -> int:
    t = build(node, thread_id)
    if t.empty:
        print(
            f"task trace: nothing recorded for thread {thread_id!r} on roster {node}\n"
            f"   (try: r4t task list --node {node})",
            file=sys.stderr,
        )
        return 1
    if json_mode:
        print(json.dumps(payload(t), indent=2))
        return 0
    for line in render(t):
        print(line)
    return 0
