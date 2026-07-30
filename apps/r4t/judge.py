"""Post-hoc failure judge — grades a finished run against the MAST taxonomy.

MAST is the Multi-Agent System Failure Taxonomy from "Why Do Multi-Agent LLM
Systems Fail?" (arXiv:2503.13657): 14 failure modes in 3 categories, derived
from 200+ annotated execution traces. The mode names and definitions below are
restated from the published paper; the judge prompt itself is original text
(the authors' repository carries no license, so none of their prompt text is
copied), grounded with worked examples from this repo's own recorded runs.
One extra mode, FM-R.1 mutual-wait deadlock, is an r4t extension — a recurring
real failure here that MAST has no single mode for.

The judge is a measurement instrument for humans. It runs only after a run,
reads only recorded state (per-member turn captures, the node log, dead
letters), and persists its reports under the team dir's judge/ — never inside
the org's workplace, never anywhere a roster agent reads. Org agents must
never see judge output: a graded org changes behavior, and an agent that can
read its grader can game it.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import state
from dispatch import run_harness
from rig import RigError, load_rig_config

CHUNK_MAX_CHARS = 48_000
LOG_TAIL_LINES = 200
EVIDENCE_LINE_MAX = 200


@dataclass(frozen=True)
class Mode:
    id: str
    name: str
    category: str
    definition: str


CATEGORIES = [
    ("FC1", "Specification issues"),
    ("FC2", "Inter-agent misalignment"),
    ("FC3", "Task verification"),
    ("R4T", "r4t extension (not MAST)"),
]

MODES = [
    Mode("FM-1.1", "Disobey task specification", "FC1",
         "output or actions violate an explicit constraint, requirement, or "
         "ceiling of the assigned task"),
    Mode("FM-1.2", "Disobey role specification", "FC1",
         "a member acts outside its declared role — a reviewer implementing, "
         "a worker issuing leadership decisions"),
    Mode("FM-1.3", "Step repetition", "FC1",
         "a step already completed is redone without new input or reason"),
    Mode("FM-1.4", "Loss of conversation history", "FC1",
         "a member proceeds as if earlier exchanged context never happened, "
         "reverting to a stale state"),
    Mode("FM-1.5", "Unaware of termination conditions", "FC1",
         "work or conversation continues past the point the task's stop "
         "condition was already met"),
    Mode("FM-2.1", "Conversation reset", "FC2",
         "a dialogue restarts from scratch mid-run, discarding progress "
         "already made"),
    Mode("FM-2.2", "Fail to ask for clarification", "FC2",
         "a member proceeds on ambiguous or contradictory input instead of "
         "asking the counterparty"),
    Mode("FM-2.3", "Task derailment", "FC2",
         "effort drifts to work that is not part of the assigned objective"),
    Mode("FM-2.4", "Information withholding", "FC2",
         "a member holds information another member needs and does not "
         "share it"),
    Mode("FM-2.5", "Ignored other agent's input", "FC2",
         "another member's message or correction goes unused — consuming a "
         "message and ending the turn with no reply and no resulting action "
         "counts"),
    Mode("FM-2.6", "Reasoning-action mismatch", "FC2",
         "stated reasoning and actual action diverge — the member says one "
         "thing and does another"),
    Mode("FM-3.1", "Premature termination", "FC3",
         "a task, turn, or exchange ends before the objective is met or the "
         "needed information has been exchanged"),
    Mode("FM-3.2", "No or incomplete verification", "FC3",
         "work is declared done without checking, or with a check too "
         "shallow to catch obvious defects"),
    Mode("FM-3.3", "Incorrect verification", "FC3",
         "a verification step passes something wrong, or asserts an approval "
         "that never happened"),
    Mode("FM-R.1", "Mutual-wait deadlock", "R4T",
         "two parties each wait on the other for the next move; the thread "
         "sits silent although neither is actually blocked (r4t extension, "
         "not part of MAST)"),
]

MODE_BY_ID = {m.id: m for m in MODES}

EXAMPLES = """\
Worked examples from previously graded runs (the pattern matters, not the
names):

1. FM-3.3 — the leader committed a status doc recording the milestone as
   "blessed by the owner" before the owner had reviewed anything. Approval
   was asserted, not received.
2. FM-3.2 — a member announced a deliverable as "perfectly validated" on the
   strength of a smoke test that ran for 0.29 seconds.
3. FM-R.1 — two members each ended their turns stating they were waiting on
   the other; the thread sat silent until an outside nudge broke the tie.
   Neither was actually blocked.
4. FM-2.5 — several members consumed their queued messages and ended their
   turns without replying and without committing any work; the senders were
   left assuming their requests were in progress.
5. FM-1.1 — the mission set a 15-20k word ceiling; a member confidently
   delivered 44k words in one sitting.
6. FM-2.3 — the org designed and began building a feature that appears
   nowhere in the mission file, then requested a blessing for it.\
"""


FALLBACK_NOTE = """\
Delivery semantics you must apply before grading "printed instead of sending":
this team has a stdout fallback. A turn that ends without staging any message
but prints a non-trivial prose answer does NOT lose that answer — the cleaned
stdout is delivered as ONE reply to the sender of the message the turn was
answering. Prose-only output is therefore a normal, delivered reply, not a
dropped one. Do not raise FM-1.1, FM-2.5, or FM-2.6 merely because a member
answered in prose without an explicit send.

Prose-only output IS a genuine failure only when one of these holds:
- the output narrates messages to recipients other than (or in addition to)
  the one inbound sender — the fallback reaches the inbound sender alone, so
  "I told Rook and Faye and Neil" as prose reaches only whoever last wrote in;
  the other named recipients never receive it;
- the turn is genuinely silent or chrome-only — no answer worth relaying, so
  nothing is delivered at all; or
- a required non-message action (writing a file, running a command) is
  narrated as done but was not actually performed in the turn.
Grade those normally. A plain prose answer to the inbound sender is delivered
and is not a finding on its own."""


@dataclass
class Unit:
    member: str
    turn: str
    text: str


@dataclass
class Finding:
    mode: str
    member: str
    turn: str
    evidence: str


@dataclass
class Result:
    node: str
    rig: str
    stamp: str
    units: int
    members: list[str]
    invokes: int
    findings: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class JudgeError(Exception):
    """Operational failure (exit 2): nothing to judge, a missing rig, or a
    rig invoke that returned nothing gradable."""


def judge_dir(node: str) -> Path:
    return state.team_dir(node) / "judge"


def collect_units(node: str) -> list[Unit]:
    agents_root = state.team_dir(node) / "agents"
    if not agents_root.is_dir():
        return []
    units: list[Unit] = []
    for entry in sorted(agents_root.iterdir()):
        if not entry.is_dir():
            continue
        member = entry.name
        for path in state.list_turn_captures(node, member):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            units.append(Unit(member=member, turn=path.stem, text=text))
    return units


def run_context(node: str) -> Unit | None:
    parts: list[str] = []
    lines = state.recent_log_lines(node, days=2)
    if lines:
        parts.append("node log (tail):\n" + "\n".join(lines[-LOG_TAIL_LINES:]))
    letters = state.list_dead_letters(node)
    if letters:
        rows = [
            f"- {d.get('reason', '?')}: {d.get('from', '?')} -> "
            f"{d.get('to', '?')} thread={d.get('thread', '?')}"
            for d in letters
        ]
        parts.append("dead letters:\n" + "\n".join(rows))
    if not parts:
        return None
    return Unit(member="(node)", turn="run-context", text="\n\n".join(parts))


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    marker = "\n[... truncated by judge ...]\n"
    head = int((cap - len(marker)) * 0.6)
    tail = cap - len(marker) - head
    return text[:head] + marker + text[-tail:]


def _render_unit(unit: Unit) -> str:
    return (
        f"=== member: {unit.member} | turn: {unit.turn} ===\n"
        f"{_truncate(unit.text, CHUNK_MAX_CHARS)}"
    )


def pack_chunks(units: list[Unit]) -> list[str]:
    """Greedy pack of rendered units into transcript chunks so each judge
    invoke stays a one-shot prompt; an oversized capture is truncated
    middle-out rather than dropped."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for unit in units:
        rendered = _render_unit(unit)
        if current and size + len(rendered) > CHUNK_MAX_CHARS:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(rendered)
        size += len(rendered)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _taxonomy_block() -> str:
    lines: list[str] = []
    for cat, label in CATEGORIES:
        lines.append(f"{cat} {label}:")
        for mode in MODES:
            if mode.category == cat:
                lines.append(f"  {mode.id} {mode.name}: {mode.definition}.")
    return "\n".join(lines)


def build_prompt(chunk: str, *, part: int, parts: int) -> str:
    return f"""\
You are a failure-mode judge grading recorded transcripts from a finished
multi-agent AI team run. Each transcript below is one member's turn: the
verbatim prompt it received and the raw output it produced. You are reading
part {part} of {parts}.

Grade ONLY against this taxonomy (MAST, arXiv:2503.13657, plus one marked
r4t extension):

{_taxonomy_block()}

{EXAMPLES}

{FALLBACK_NOTE}

Rules:
- Report a finding only when the transcript text in front of you shows it.
  Do not speculate about turns you cannot see.
- One finding per (mode, member, turn) occurrence; the same turn may exhibit
  several modes.
- Copy `member` and `turn` exactly from the transcript header it came from.
- `evidence` is one or two short sentences quoting or tightly paraphrasing
  the transcript.
- No findings is a valid answer.

Respond with ONLY a JSON object, no prose before or after:
{{"findings": [{{"mode": "FM-x.y", "member": "...", "turn": "...", "evidence": "..."}}]}}

Transcripts:

{chunk}
"""


def _json_candidates(output: str):
    yield output.strip()
    for fenced in re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", output, re.DOTALL):
        yield fenced
    start, end = output.find("{"), output.rfind("}")
    if start != -1 and end > start:
        yield output[start:end + 1]


def parse_findings(output: str) -> list[dict] | None:
    for candidate in _json_candidates(output):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("findings"), list):
            return [f for f in data["findings"] if isinstance(f, dict)]
    return None


def _one_line(value: object, cap: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:cap]


def _resolve_rig(rig_name: str, config_path: Path):
    try:
        config = load_rig_config(config_path)
    except RigError as e:
        raise JudgeError(f"judge: cannot read rig config: {e}") from e
    hint = f"(try: r4t rig add {rig_name} <preset>; presets: r4t rig presets)"
    if config.missing:
        raise JudgeError(f"judge: no rig config at {config_path}   {hint}")
    rig = config.rigs.get(rig_name.strip().lower())
    if rig is None:
        raise JudgeError(
            f"judge: rig {rig_name!r} not found in {config_path}   {hint}"
        )
    if rig.error:
        raise JudgeError(
            f"judge: rig {rig_name!r} is invalid: {rig.error}   "
            f"(try: r4t rig configure {rig_name})"
        )
    return rig


def _invoke_chunks(rig, chunks: list[str], cwd: Path, result: Result) -> None:
    parsed_any = False
    for i, chunk in enumerate(chunks, 1):
        prompt = build_prompt(chunk, part=i, parts=len(chunks))
        code, output, _duration, timed_out = run_harness(rig, prompt, cwd)
        result.invokes += 1
        if timed_out:
            raise JudgeError(
                f"judge: rig {rig.name!r} timed out after "
                f"{rig.timeout_seconds:g}s on part {i}/{len(chunks)}   "
                f"(try: r4t rig set {rig.name} timeout_seconds <secs>)"
            )
        if code != 0:
            first = _one_line(output, 160) or "(no output)"
            raise JudgeError(
                f"judge: rig {rig.name!r} invoke failed (exit {code}) on part "
                f"{i}/{len(chunks)}: {first}   (try: r4t rig list)"
            )
        raw = parse_findings(output)
        if raw is None:
            result.warnings.append(
                f"part {i}/{len(chunks)}: no parsable JSON verdict; skipped"
            )
            continue
        parsed_any = True
        for f in raw:
            mode = _one_line(f.get("mode"), 16).upper()
            if mode not in MODE_BY_ID:
                result.warnings.append(
                    f"part {i}/{len(chunks)}: unknown mode {mode!r} dropped"
                )
                continue
            result.findings.append(Finding(
                mode=mode,
                member=_one_line(f.get("member"), 64) or "?",
                turn=_one_line(f.get("turn"), 128) or "?",
                evidence=_one_line(f.get("evidence"), EVIDENCE_LINE_MAX),
            ))
    if not parsed_any:
        raise JudgeError(
            f"judge: rig {rig.name!r} returned no parsable verdict for any "
            f"part   (try: a rig on a stronger model, then rerun)"
        )


def _counts(result: Result) -> dict[str, list[Finding]]:
    by_mode: dict[str, list[Finding]] = {m.id: [] for m in MODES}
    for f in result.findings:
        by_mode[f.mode].append(f)
    return by_mode


def render_report(result: Result, report_path: Path) -> str:
    by_mode = _counts(result)
    lines = [
        f"judge: {result.node}",
        f"rig: {result.rig}",
        f"graded: {result.units} turn capture(s) across "
        f"{len(result.members)} member(s), {result.invokes} invoke(s)",
        f"report: {report_path}",
        "",
    ]
    width = max(len(f"{m.id} {m.name}") for m in MODES)
    for cat, label in CATEGORIES:
        lines.append(f"{cat}  {label}")
        for mode in MODES:
            if mode.category != cat:
                continue
            found = by_mode[mode.id]
            mark = "✗" if found else "✓"
            title = f"{mode.id} {mode.name}"
            lines.append(f"  {mark} {title:<{width}}  {len(found)}")
            for f in found:
                lines.append(f"      {f.member} / {f.turn}: {f.evidence}")
        lines.append("")
    lines.append("Summary")
    hit = sum(1 for fs in by_mode.values() if fs)
    if result.findings:
        lines.append(
            f"  ✗ {len(result.findings)} finding(s) across {hit} mode(s)"
        )
    else:
        lines.append("  ✓ no findings")
    if result.warnings:
        lines.append("")
        lines.append("Warnings")
        for w in result.warnings:
            lines.append(f"  ⚠ {w}")
    return "\n".join(lines) + "\n"


def report_payload(result: Result) -> dict:
    by_mode = _counts(result)
    return {
        "node": result.node,
        "rig": result.rig,
        "generated": state.utc_now(),
        "stamp": result.stamp,
        "turns": result.units,
        "members": result.members,
        "invokes": result.invokes,
        "modes": {
            m.id: {
                "name": m.name,
                "category": m.category,
                "count": len(by_mode[m.id]),
                "evidence": [
                    {"member": f.member, "turn": f.turn, "evidence": f.evidence}
                    for f in by_mode[m.id]
                ],
            }
            for m in MODES
        },
        "total_findings": len(result.findings),
        "warnings": result.warnings,
    }


def run(
    node: str,
    *,
    rig_name: str,
    config_path: Path,
    json_mode: bool = False,
    out=None,
    err=None,
) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    units = collect_units(node)
    if not units:
        print(
            f"judge: no turn captures for node {node!r} under "
            f"{state.team_dir(node) / 'agents'}   "
            f"(try: run the org first; captures land in "
            f"agents/<member>/turns/)",
            file=err,
        )
        return 2
    members = sorted({u.member for u in units})
    context = run_context(node)
    if context is not None:
        units = units + [context]

    home = judge_dir(node)
    home.mkdir(parents=True, exist_ok=True)
    result = Result(
        node=node,
        rig=rig_name,
        stamp=state.turn_capture_stamp(),
        units=len(units) - (1 if context is not None else 0),
        members=members,
        invokes=0,
    )
    try:
        rig = _resolve_rig(rig_name, config_path)
        _invoke_chunks(rig, pack_chunks(units), home, result)
    except JudgeError as e:
        print(str(e), file=err)
        return 2

    report_path = home / f"{result.stamp}-report.md"
    text = render_report(result, report_path)
    payload = report_payload(result)
    report_path.write_text(text, encoding="utf-8")
    state.atomic_write_json(home / f"{result.stamp}-report.json", payload)
    if json_mode:
        print(json.dumps(payload, indent=2), file=out)
    else:
        print(text, end="", file=out)
    return 0
