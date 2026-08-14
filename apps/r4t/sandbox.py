"""`r4t sandbox` — disposable end-to-end roster run with a graded report.

Creates a temp dir holding a private A8S_HOME + R4T_HOME, copies the
bundled roster-of-3 seed (apps/r4t/sandbox/) into a temp repo, registers the
node + namespace through the real a8s CLI, starts a handler, kicks off the
GOAL.md task as a registered "human" agent, waits for quiescence, tears
everything down (a8s stop is a graceful SIGTERM; the no-orphans invariant
is verified with a process scan), and writes one self-contained markdown
report whose MECHANICAL CHECKS section is computed — an external judge
needs nothing but the report. Progress logs go to stderr; the final report
is written to stdout (pipe or redirect to save it).

`--fake` swaps every rig's invoke for sandbox/fake-agent.py: scripted
role-play that exercises dispatch, staging release, header stamping,
delegation, and the final leader answer with zero LLM calls. Live mode uses
`--preset` (any `r4t rig presets` entry; default `opencode`) and
optional `--model` for presets like `ollama-opencode`. The chosen argv is
passed to live-agent.py via R4T_SANDBOX_INVOKE.

`--break MEMBER[:SHAPE]` pins one member to a deliberately broken rig. Each
shape is a different way real harnesses fail, and each lands on a different
recovery path (see FAILURE_SHAPES); the mechanical checks assert the path,
so a governance regression turns the run red.

What fake mode deliberately fakes vs live mode is listed in
docs/r4t-development.md — every divergence is a place `--fake` can pass
while a real roster misbehaves.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import state

from rig import RigError, build_preset_invoke, format_preset_invoke, preset_names

# The isolation test (apps/r4t/tests/docker/run-as.sh) copies apps/r4t alone
# into a container with no repo root, so `ark` is not always reachable there.
try:
    from ark.proc import terminate_group as _terminate_group
except ImportError:
    def _terminate_group(pid: int, *, grace_seconds: float = 0.5) -> None:
        # Mirrors ark.proc.terminate_group: the pgid is resolved once, before
        # SIGTERM, so a leader that exits during the grace period cannot
        # strand SIGKILL with no pid left to resolve; pid stands in as the
        # pgid when getpgid cannot answer (true for any start_new_session
        # leader).
        if os.name != "posix":
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            return
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = pid
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except OSError:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            if sig is signal.SIGTERM:
                time.sleep(grace_seconds)

R4T_DIR = Path(__file__).resolve().parent
SANDBOX_DIR = R4T_DIR / "sandbox"
A8S_DIR = R4T_DIR.parent / "a8s"
A8S_PY = A8S_DIR / "a8s.py"

ROSTER = "trio"
NODE = "trio-node"
ALIAS = "sandboxtrio"
MAX_TURNS = 15


FAILURE_SHAPES = {
    "exit": "rig always exits nonzero — breaker trips, queue holds",
    "hang": "rig sleeps past its timeout — turn killed, batch requeued",
    "silent": "member works but answers on stdout, never tells — stdout fallback",
    "mute": "member's first turn stages nothing — quiet sweep nudges the leader",
}
FAILURE_RIG_NAMES = {"exit": "broken", "hang": "hung", "silent": "silent", "mute": "mute"}
FAILURE_BUDGET_MAX = 10
QUIET_TASK_SECONDS = 10


class SandboxError(Exception):
    pass


def _log(msg: str) -> None:
    print(f"sandbox: {msg}", file=sys.stderr, flush=True)


def _a8s(*args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, str(A8S_PY), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise SandboxError(
            f"a8s {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def _write_definition(path: Path) -> None:
    state.atomic_write_json(
        path,
        {
            "description": "r4t sandbox node",
            "invoke": [
                sys.executable,
                str(R4T_DIR / "r4t.py"),
                "dispatch",
                "--root",
                ".",
                "--from",
                "$SENDER",
                "--to",
                "$RECIPIENT",
                "--message",
                "$MESSAGE",
            ],
            "max_wake_seconds": 2700,
            "idle": {
                "timeout": 10,
                "invoke": [
                    sys.executable,
                    str(R4T_DIR / "r4t.py"),
                    "idle",
                    "--root",
                    ".",
                    "--node",
                    ROSTER,
                ],
            },
        },
    )


def parse_break(spec: str) -> tuple[str, str]:
    member, _, shape = spec.strip().partition(":")
    shape = (shape or "exit").lower()
    if not member:
        raise SandboxError("--break needs a member name, e.g. --break dev:hang")
    if shape not in FAILURE_SHAPES:
        raise SandboxError(
            f"unknown break shape {shape!r} — pick one of "
            f"{', '.join(sorted(FAILURE_SHAPES))}"
        )
    return member.lower(), shape


def _failure_rig(shape: str) -> dict:
    if shape == "exit":
        invoke = [sys.executable, "-c", "import sys; sys.exit(1)", "{prompt}"]
        timeout = 30.0
    elif shape == "hang":
        # Sleeps far past its own timeout so the turn can only end one way:
        # killed by r4t. A sleep near the timeout would race the check.
        invoke = [sys.executable, "-c", "import time; time.sleep(300)", "{prompt}"]
        timeout = 3.0
    else:
        invoke = [
            sys.executable,
            str(SANDBOX_DIR / "fake-agent.py"),
            "{prompt}",
            f"--{shape}",
        ]
        timeout = 60.0
    return {
        "invoke": invoke,
        "timeout_seconds": timeout,
        "budget_max": FAILURE_BUDGET_MAX,
        "budget_earn_per_hour": FAILURE_BUDGET_MAX,
    }


def _write_rig_config(
    path: Path, fake: bool, failure: tuple[str, str] | None = None
) -> None:
    config = json.loads((SANDBOX_DIR / "rigs.json").read_text(encoding="utf-8"))
    if fake:
        for value in config.values():
            if isinstance(value, dict) and "invoke" in value:
                value["invoke"] = [
                    sys.executable,
                    str(SANDBOX_DIR / "fake-agent.py"),
                    "{prompt}",
                ]
                value["timeout_seconds"] = 60
        config["throttle"] = {"max_concurrent": 1, "min_seconds_between_turn_starts": 0}
    else:
        for value in config.values():
            if isinstance(value, dict) and "invoke" in value:
                value["invoke"] = [
                    sys.executable,
                    str(SANDBOX_DIR / "live-agent.py"),
                    "{prompt}",
                ]
    if failure:
        member, shape = failure
        rig_name = FAILURE_RIG_NAMES[shape]
        config[rig_name] = _failure_rig(shape)
        config["pins"] = {member: rig_name}
        # Kept low for every shape, not just the failing ones: a shape that
        # exits 0 must reach its recovery path with the breaker still closed,
        # and a cap of 2 makes any stray trip visible.
        config["breaker_cap"] = 2
        if shape == "mute":
            config["quiet_task_seconds"] = QUIET_TASK_SECONDS
    state.atomic_write_json(path, config)


def _kickoff(human_root: Path, goal: str) -> None:
    outbox = human_root / ".outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    msg_id = f"{time.time_ns():026d}"
    state.atomic_write_json(
        outbox / f"{msg_id}.json",
        {
            "id": msg_id,
            "to": f"{ROSTER}:lead",
            "content": (
                "Build the battleship game in GOAL.md.\n\n"
                "Lead: your only action this turn is to delegate — run:\n"
                f'  tell {ROSTER}:dev "Build battleship.py per GOAL.md in this directory."\n'
                "Do not implement it yourself.\n\n" + goal
            ),
            "files": [],
        },
    )


def _agent_messages(a8s_home: Path, agent: str) -> list[dict]:
    out: list[dict] = []
    for sub in ("inbox", "trash"):
        d = a8s_home / "agents" / agent / sub
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                out.append(data)
    return out


def _final_answer(a8s_home: Path) -> dict | None:
    for msg in _agent_messages(a8s_home, "human"):
        sender = str(msg.get("from", ""))
        # The human seat is outside the walls, so the router presents the org's
        # answer as the bare prefix; NODE and `ROSTER:member` are the shapes a
        # message takes before it crosses.
        if sender not in (ROSTER, NODE) and not sender.startswith(f"{ROSTER}:"):
            continue
        if str(msg.get("content", "")).strip():
            return msg
    return None


def _busy(a8s_home: Path, repo: Path, *, parked: str = "") -> bool:
    if state.live_locks(ROSTER):
        return True
    if any(
        state.queue_depth(ROSTER, m)
        for m in state.members_with_queue(ROSTER)
        if m != parked
    ):
        return True
    for d in (
        a8s_home / "agents" / NODE / "inbox",
        a8s_home / "agents" / "human" / "inbox",
        repo / ".outbox",
        a8s_home / "agents" / "human" / ".outbox",
    ):
        if d.is_dir() and any(d.glob("*.json")):
            return True
    return False


def _handler_pids(a8s_home: Path) -> list[int]:
    pids = []
    for pid_file in (a8s_home / "agents").glob("*/pid"):
        try:
            pids.append(int(pid_file.read_text().strip()))
        except (OSError, ValueError):
            continue
    return pids


def _stop_handlers(a8s_home: Path) -> None:
    try:
        _a8s("stop", ALIAS)
    except SandboxError:
        pass
    deadline = time.time() + 30
    while time.time() < deadline and _handler_pids(a8s_home):
        time.sleep(0.5)
    for agent in (NODE, "human"):
        if (a8s_home / "agents" / agent / "pid").is_file():
            try:
                _a8s("kill", agent)
            except SandboxError:
                pass
    deadline = time.time() + 15
    while time.time() < deadline and _handler_pids(a8s_home):
        time.sleep(0.5)


def _orphans(tmp: Path) -> list[str]:
    result = subprocess.run(
        ["ps", "-ax", "-o", "pid=,command="], capture_output=True, text=True
    )
    needle = str(tmp)
    return [line.strip() for line in result.stdout.splitlines() if needle in line]


def _kill_sandbox_processes(tmp: Path) -> int:
    """Terminate any process still referencing the temp sandbox dir."""
    lines = _orphans(tmp)
    pids: list[int] = []
    for line in lines:
        try:
            pids.append(int(line.split(None, 1)[0]))
        except (ValueError, IndexError):
            continue
    if not pids:
        return 0
    for pid in pids:
        _terminate_group(pid, grace_seconds=2)
    return len(pids)


def _run_program(repo: Path) -> tuple[bool, str]:
    candidates = sorted(repo.glob("*.py"))
    if not candidates:
        return False, "no program file to run"
    program = next((p for p in candidates if "battleship" in p.name), candidates[0])
    guesses = "\n".join(f"{r} {c}" for r in range(5) for c in range(5)) + "\n"
    try:
        result = subprocess.run(
            [sys.executable, str(program)],
            input=guesses,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(repo),
        )
    except subprocess.TimeoutExpired:
        return False, f"{program.name} timed out after 30s"
    ok = result.returncode == 0
    tail = (result.stdout or "").strip().splitlines()[-1:] or [""]
    return ok, f"{program.name} exited {result.returncode} ({tail[0]})"


def _velocity_rows() -> list[list[str]]:
    path = state.roster_dir(ROSTER) / "velocity.csv"
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return [line.split(",") for line in lines[1:]]


def _governance_lines() -> list[str]:
    lines: list[str] = []
    log_dir = state.roster_dir(ROSTER) / "log"
    if not log_dir.is_dir():
        return lines
    for f in sorted(log_dir.glob("*.md")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.startswith("r4t: "):
                lines.append(line)
    return lines


HISTORY_ENTRY_RE = re.compile(
    r"(?m)^## (\S+) (from|to) (\S+)\n\n(.*?)(?=\n## |\Z)", re.DOTALL
)


def _conversation() -> list[tuple[str, str, str, str]]:
    """(timestamp, sender, recipient, body) from every agent's history.
    Intra-roster messages appear once (the recipient's `from` entry); external
    releases come from `to` entries addressed outside the roster."""
    events: list[tuple[str, str, str, str]] = []
    agents_dir = state.roster_dir(ROSTER) / "agents"
    if not agents_dir.is_dir():
        return events
    for history in agents_dir.glob("*/history.md"):
        agent = f"{ROSTER}:{history.parent.name}"
        for ts, direction, other, body in HISTORY_ENTRY_RE.findall(
            history.read_text(encoding="utf-8")
        ):
            if direction == "from":
                events.append((ts, other, agent, body.strip()))
            elif not other.lower().startswith(ROSTER):
                events.append((ts, agent, other, body.strip()))
    events.sort(key=lambda e: e[0])
    return events


def _dead_letter_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in state.list_dead_letters(ROSTER):
        reason = record.get("reason", "?")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _emit_progress(
    *,
    seen_velocity: int,
    seen_gov: set[str],
    seen_locks: set[tuple[str, str]],
) -> tuple[int, set[str], set[tuple[str, str]]]:
    rows = _velocity_rows()
    for row in rows[seen_velocity:]:
        if len(row) >= 7:
            _log(f"turn done: {row[1]} ({row[2]}) task={row[3][:8]}… "
                 f"exit={row[6]} in {row[5]}s")
        else:
            _log(f"turn done: {', '.join(row)}")
    seen_velocity = len(rows)

    for line in _governance_lines():
        if line not in seen_gov:
            _log(line)
            seen_gov.add(line)

    for lock in state.live_locks(ROSTER, prune=True):
        key = (str(lock.get("agent", "")), str(lock.get("task", "")))
        if key not in seen_locks:
            seen_locks.add(key)
            _log(
                f"turn started: {lock.get('agent', '?')} "
                f"(rig {lock.get('rig', '?')}, task {str(lock.get('task', ''))[:8]}…)"
            )
    return seen_velocity, seen_gov, seen_locks


def _parked_member(failure: tuple[str, str] | None) -> str:
    """The member whose queue is meant to sit still. Once its breaker has
    tripped, the held message IS the scenario's end state — counting it as work
    in flight would keep the run waiting for a turn that can never come."""
    if not failure or failure[1] not in ("exit", "hang"):
        return ""
    member = failure[0]
    if any(f"BREAKER {member} tripped" in line for line in _governance_lines()):
        return member
    return ""


def _scenario_pending(failure: tuple[str, str] | None) -> bool:
    """A failure scenario is over when the recovery path it targets has fired,
    not when the roster first goes quiet — the leader's early ack to the human
    would otherwise end the run before the failure had any consequence."""
    if not failure:
        return False
    member, shape = failure
    gov = _governance_lines()
    if shape in ("exit", "hang"):
        return not (
            any("BREAKER" in line and "tripped" in line for line in gov)
            and any("BREAKER" in line and "breaker open" in line for line in gov)
        )
    if shape == "silent":
        return not any(f"STDOUT-REPLY {member}" in line for line in gov)
    return not any("QUIET thread=" in line for line in gov)


def _gov_detail(lines: list[str]) -> str:
    return lines[0].split("r4t: ", 1)[-1]


def _failure_checks(
    failure: tuple[str, str], turns: int
) -> list[tuple[str, object, str]]:
    """Mechanical assertions for one failure shape: each names the recovery
    path r4t is supposed to take, so a governance regression reads as FAIL."""
    member, shape = failure
    gov = _governance_lines()
    tripped = [line for line in gov if "BREAKER" in line and "tripped" in line]
    held = [line for line in gov if "BREAKER" in line and "breaker open" in line]
    level = state.budget_level(ROSTER, member, FAILURE_BUDGET_MAX, FAILURE_BUDGET_MAX)
    checks: list[tuple[str, object, str]] = []

    if shape == "exit":
        checks += [
            (
                "Breaker tripped",
                bool(tripped),
                f"{member} pinned to an always-failing rig (breaker_cap 2)",
            ),
            (
                "Breaker held queued message(s)",
                bool(held),
                "messages hold in the queue while the breaker is open — none dropped",
            ),
        ]
    elif shape == "hang":
        killed = [
            line
            for line in gov
            if line.startswith(f"r4t: RETRY {member}") and "killed at timeout" in line
        ]
        requeued = [line for line in killed if "returned to the queue" in line]
        checks += [
            (
                "Turn killed at its timeout",
                bool(killed),
                _gov_detail(killed) if killed else f"{member} never recorded a timed-out turn",
            ),
            (
                "Timed-out batch requeued",
                bool(requeued),
                "a killed turn returns its whole batch to the queue — nothing lost",
            ),
            (
                "Breaker tripped on timeouts",
                bool(tripped),
                "repeated timeouts count as failures until the breaker trips",
            ),
            (
                "Breaker held queued message(s)",
                bool(held),
                "messages hold in the queue while the breaker is open — none dropped",
            ),
        ]
    elif shape == "silent":
        relayed = [line for line in gov if f"STDOUT-REPLY {member}" in line]
        checks += [
            (
                "Stdout answer relayed as a reply",
                bool(relayed),
                _gov_detail(relayed) if relayed else f"{member}'s stdout reached nobody",
            ),
            (
                "Breaker stayed closed",
                not tripped,
                "answering on stdout is not a failed turn — exit 0 must not trip it",
            ),
        ]
    else:
        silent = [line for line in gov if f"SILENT {member}" in line]
        nudged = [line for line in gov if "QUIET thread=" in line]
        checks += [
            (
                "Silent turn logged",
                bool(silent),
                _gov_detail(silent) if silent else f"{member} staged nothing and r4t said nothing",
            ),
            (
                # The kickoff arrives through the human's a8s node, so every
                # thread in this run descends from ingress and is owed
                # nothing. Silence here is the ruling working, not a dropped
                # ball — the sweep watches threads that begin inside.
                "Quiet sweep left the ingress thread alone",
                not nudged,
                _gov_detail(nudged) if nudged else "no ingress thread was swept",
            ),
            (
                "Breaker stayed closed",
                not tripped,
                "a turn that exits 0 without staging is not a failure",
            ),
        ]

    checks.append(
        (
            f"Budget charged for {member}'s turn(s)",
            turns > 0 and level < FAILURE_BUDGET_MAX,
            f"member budget {state.fmt_budget(level)} of {FAILURE_BUDGET_MAX} — "
            "a turn is charged at admission, however it ends",
        )
    )
    return checks


def _build_report(
    *,
    mode: str,
    wall_clock: float,
    checks: list[tuple[str, object, str]],
    goal: str,
    repo_files: dict[str, str],
    harness: str = "",
) -> str:
    lines = [
        f"# r4t sandbox report — {mode} run",
        "",
        f"Generated {state.utc_now()} by `r4t sandbox`. Self-contained: the",
        "mechanical section is computed by the runner; everything else is the",
        "raw record of the run.",
        "",
        "## Mechanical checks",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
    ]
    for name, result, detail in checks:
        if isinstance(result, bool):
            shown = "PASS" if result else "FAIL"
        else:
            shown = str(result)
        lines.append(f"| {name} | {shown} | {detail} |")
    lines += [
        "",
        "## Run",
        "",
        f"- mode: {mode}",
    ]
    if harness:
        lines.append(f"- harness: {harness}")
    lines += [
        f"- wall clock: {wall_clock:.1f}s",
        "",
        "### Turns (velocity)",
        "",
        "| time | agent | rig | task | hop | seconds | exit |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in _velocity_rows():
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", "## Scenario (GOAL.md)", "", goal.strip(), "", "## Conversation", ""]
    for ts, sender, recipient, body in _conversation():
        lines.append(f"**{sender} → {recipient}** ({ts})")
        lines.append("")
        lines.extend(f"> {line}" for line in body.splitlines())
        lines.append("")
    lines += ["## Governance events", ""]
    events = _governance_lines()
    if events:
        lines.extend(f"- `{line}`" for line in events)
    else:
        lines.append("(none)")
    lines += ["", "## Produced files", ""]
    if repo_files:
        for name, content in sorted(repo_files.items()):
            lines += [f"### {name}", "", "```python", content.rstrip(), "```", ""]
    else:
        lines.append("(none)")
    return "\n".join(lines) + "\n"


def run_sandbox(
    *,
    fake: bool,
    timeout: float,
    preset: str = "opencode",
    model: str | None = None,
    break_member: str | None = None,
) -> int:
    start = time.time()
    failure: tuple[str, str] | None = None
    if break_member:
        try:
            failure = parse_break(break_member)
        except SandboxError as e:
            _log(str(e))
            return 1
    tmp = Path(tempfile.mkdtemp(prefix="r4t-sandbox-"))
    saved_env = {k: os.environ.get(k) for k in ("A8S_HOME", "R4T_HOME", "R4T_SANDBOX_INVOKE", "R4T_SANDBOX")}
    a8s_home = tmp / "a8s-home"
    os.environ["A8S_HOME"] = str(a8s_home)
    os.environ["R4T_HOME"] = str(tmp / "r4t-home")
    os.environ["R4T_SANDBOX"] = "1"
    mode = "fake" if fake else "live"
    if failure:
        mode += f"+break:{failure[0]}:{failure[1]}"
        _log(f"break {failure[0]}: {FAILURE_SHAPES[failure[1]]}")
    harness_line = ""
    seen_velocity = 0
    seen_gov: set[str] = set()
    seen_locks: set[tuple[str, str]] = set()
    try:
        if fake:
            os.environ.pop("R4T_SANDBOX_INVOKE", None)
            _log("mode=fake (deterministic agents, no LLM)")
        else:
            try:
                invoke = build_preset_invoke(preset, model=model)
            except RigError as e:
                _log(str(e))
                return 1
            os.environ["R4T_SANDBOX_INVOKE"] = json.dumps(invoke)
            harness_line = format_preset_invoke(preset.strip().lower())
            if model:
                harness_line = f"{preset} (model={model}) — {harness_line}"
            else:
                harness_line = f"{preset} — {harness_line}"
            _log(f"harness {harness_line}")
        repo = tmp / "repo"
        repo.mkdir(parents=True)
        seed_names = {"ROSTER.md", "GOAL.md"}
        for name in seed_names:
            shutil.copy(SANDBOX_DIR / name, repo / name)
        workspace = repo.resolve()
        (repo / "WORKSPACE.md").write_text(
            f"# Workspace\n\nRoster repo root: `{workspace}`\n\n"
            "Write all project files here using relative paths (e.g. "
            "`battleship.py`). Do not write to ~/ or any path outside this "
            "directory.\n",
            encoding="utf-8",
        )
        goal = (repo / "GOAL.md").read_text(encoding="utf-8")

        _write_rig_config(tmp / "r4t-home" / "rigs.json", fake, failure=failure)
        definition = tmp / "r4t-def.json"
        _write_definition(definition)
        human_root = tmp / "human"
        human_root.mkdir()

        _log("registering node and handlers")
        _a8s("add", NODE, str(repo), str(definition))
        _a8s("add", "human", str(human_root), str(A8S_DIR / "definitions" / "default.json"))
        _a8s("namespace", ROSTER, NODE)
        _a8s("alias", ALIAS, NODE)
        _a8s("alias", ALIAS, "human")
        _a8s("start", ALIAS)

        _kickoff(human_root, goal)
        _log("kickoff sent to trio:lead")

        deadline = time.time() + timeout
        quiet_polls = 0
        final = None
        while True:
            now = time.time()
            parked = _parked_member(failure)
            if now >= deadline:
                if _busy(a8s_home, repo, parked=parked):
                    _log("timeout with work in flight — killing harness processes")
                    killed = _kill_sandbox_processes(tmp)
                    if killed:
                        _log(f"sent SIGTERM/SIGKILL to {killed} process(es)")
                    drain_until = now + 45
                    while time.time() < drain_until:
                        time.sleep(2)
                        seen_velocity, seen_gov, seen_locks = _emit_progress(
                            seen_velocity=seen_velocity,
                            seen_gov=seen_gov,
                            seen_locks=seen_locks,
                        )
                        final = _final_answer(a8s_home) or final
                        if not _busy(a8s_home, repo, parked=parked):
                            _log("drained after timeout kill")
                            break
                else:
                    _log("timeout reached")
                break

            time.sleep(2)
            seen_velocity, seen_gov, seen_locks = _emit_progress(
                seen_velocity=seen_velocity,
                seen_gov=seen_gov,
                seen_locks=seen_locks,
            )
            final = _final_answer(a8s_home)
            if final is not None:
                _log("leader answered the human")
            if _busy(a8s_home, repo, parked=parked):
                quiet_polls = 0
                continue
            quiet_polls += 1
            if final is not None and quiet_polls >= 2 and not _scenario_pending(failure):
                _log("quiescent with final answer")
                break
            if quiet_polls >= 20:
                _log("quiescent without final answer")
                break
            if quiet_polls == 1:
                _log("waiting for roster to finish…")

        _log("stopping handlers")
        _kill_sandbox_processes(tmp)
        _stop_handlers(a8s_home)
        orphans = _orphans(tmp)
        final = final or _final_answer(a8s_home)

        repo_files = {
            p.name: p.read_text(encoding="utf-8")
            for p in sorted(repo.glob("*.py"))
        }
        program_ok, program_detail = _run_program(repo)
        turns = len(_velocity_rows())
        dead = _dead_letter_counts()

        checks: list[tuple[str, object, str]] = []
        if failure:
            checks += _failure_checks(failure, turns)
        # A shape that only garbles how the answer travels still owes the
        # program: the member did its work, so the deliverable must survive the
        # detour. Shapes whose rig never runs the role at all cannot.
        if failure is None or failure[1] == "silent":
            checks += [
                (
                    "Program file(s) created",
                    bool(repo_files),
                    ", ".join(sorted(repo_files)) or "no .py files in repo",
                ),
                ("Program runs and exits 0", program_ok, program_detail),
            ]
        # A muted leader is the one shape with nobody left to answer: the
        # kickoff is ingress, owed nothing, and the member that would
        # have replied is the broken one. Silence is the outcome the ruling
        # buys, so the run states it rather than failing over it.
        answered_label = (
            "Leader stayed silent on an ingress thread"
            if failure and failure == ("lead", "mute")
            else "Leader answered the originator"
        )
        answered_ok = (final is None) if failure == ("lead", "mute") else (final is not None)
        checks += [
            (
                answered_label,
                answered_ok,
                (str(final.get("content", ""))[:120] if final else "no message from the node reached the human"),
            ),
            ("Turn count within budget", turns <= MAX_TURNS, f"{turns} turn(s) <= {MAX_TURNS}"),
            ("Zero orphan processes", not orphans, "; ".join(orphans) or "clean"),
            ("Dead letters", sum(dead.values()), json.dumps(dead) if dead else "none"),
        ]

        report = _build_report(
            mode=mode,
            wall_clock=time.time() - start,
            checks=checks,
            goal=goal,
            repo_files=repo_files,
            harness=harness_line,
        )
        failed = [name for name, result, _ in checks if isinstance(result, bool) and not result]
        sys.stdout.write(report)
        sys.stdout.flush()
        if failed:
            _log(f"FAILED checks: {', '.join(failed)}")
            return 1
        _log("all mechanical checks passed")
        return 0
    except SandboxError as e:
        _log(str(e))
        return 1
    finally:
        try:
            _kill_sandbox_processes(tmp)
        except Exception:
            pass
        try:
            _stop_handlers(a8s_home)
        except Exception:
            pass
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(tmp, ignore_errors=True)
