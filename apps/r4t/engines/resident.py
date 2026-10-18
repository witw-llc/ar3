"""Resident engine session — one long-running `stream-json` process per agent
(supported on `claude`, `ollama-claude`, and `agy`), fed incoming messages over
stdin and wrapped up when its timeout (default 45m) elapses. The timeout is
the session's whole life, not one turn's: the window already bounds every turn
inside it.

One detached bridge per `(dir, agent)` owns the engine process. Sessions run
one after another, never side by side: a message that lands while a session
wraps up waits in the inbox, and the bridge starts the next session from the
`STATUS.md` the wrap-up just wrote.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_AR3_LIB = str(Path(__file__).resolve().parents[3] / "lib")
if _AR3_LIB not in sys.path:
    sys.path.insert(0, _AR3_LIB)

# `serve` re-enters this file as __main__ with engines/ as sys.path[0]; r4t
# itself must be importable for `rig`, `engines.run`, and `engine_memory`.
_R4T_DIR = str(Path(__file__).resolve().parents[1])
if _R4T_DIR not in sys.path:
    sys.path.insert(0, _R4T_DIR)

from ar3.fsio import atomic_write_text
from ar3.locking import file_lock
from ar3.proc import pid_alive, process_start_token, terminate_group
from ar3.ulid import new as new_ulid

from engines.run import CLI_TELL_STEP, LESSON_LINE_RULE, MCP_TELL_STEP
from rig import MCP_CLAUDE_TOOL, _mcp_command, _mcp_servers_json

RESIDENT_ENGINES = frozenset({"agy", "claude", "ollama-claude"})
RESIDENT_DIR_NAME = ".engine-resident"
DEFAULT_RESIDENT_SECONDS = 2700
# The bridge's inbox poll. Latency of a fraction of a second costs nothing
# against a turn; a tighter loop costs CPU for the whole window.
POLL_SECONDS = 0.5
LOG_ROTATE_BYTES = 1024 * 1024
# At the end of the window: how long a turn still running may finish before
# the wrap-up turn is queued behind it, and how long the wrap-up itself gets.
WRAPUP_WAIT_SECONDS = 120
WRAPUP_TURN_SECONDS = 300

@dataclass(frozen=True)
class ParsedFrame:
    assistant_text: str = ""
    result_text: str = ""
    error_text: str = ""
    turn_completed: bool = False


@dataclass(frozen=True)
class ResidentAdapter:
    compose_argv: Callable[[list[str], dict[str, str], Path | None], list[str]]
    encode_user_turn: Callable[[str], str]
    parse_frame: Callable[[dict], ParsedFrame]
    tell_step: str


def _safe_agent_key(agent: str | None) -> str:
    raw = (agent or "").strip().lower()
    if not raw:
        return "_default"
    return re.sub(r"[^a-z0-9_-]", "_", raw)


def resident_home(dir_path: Path, agent: str | None) -> Path:
    return Path(dir_path).resolve() / RESIDENT_DIR_NAME / _safe_agent_key(agent)


def _pid_file(home: Path) -> Path:
    return home / "pid"


def _pid_start_file(home: Path) -> Path:
    return home / "pid.start"


def _state_file(home: Path) -> Path:
    return home / "state.json"


def _spec_file(home: Path) -> Path:
    return home / "spec.json"


def _inbox_dir(home: Path) -> Path:
    return home / "inbox"


def _lock_file(home: Path) -> Path:
    return home / ".lock"


def read_live_pid(home: Path) -> int | None:
    pf = _pid_file(home)
    if not pf.is_file():
        return None
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if not pid_alive(pid):
        return None
    try:
        stamped = _pid_start_file(home).read_text(encoding="utf-8").strip()
    except OSError:
        stamped = ""
    if stamped:
        current = process_start_token(pid)
        if current is not None and current != stamped:
            return None
    return pid


def _write_pid_files(home: Path, pid: int) -> None:
    home.mkdir(parents=True, exist_ok=True)
    atomic_write_text(_pid_file(home), f"{pid}\n")
    token = process_start_token(pid)
    if token:
        atomic_write_text(_pid_start_file(home), f"{token}\n")


def _clear_pid_files(home: Path, expected_pid: int | None = None) -> None:
    if expected_pid is not None:
        current = read_live_pid(home)
        if current is not None and current != expected_pid:
            return
    for p in (_pid_file(home), _pid_start_file(home), _state_file(home)):
        p.unlink(missing_ok=True)


def read_state(home: Path) -> dict | None:
    sf = _state_file(home)
    if not sf.is_file():
        return None
    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_state(home: Path, *, pid: int, engine_pid: int | None, status: str) -> None:
    """`status` is `starting`, `running`, `closing` or `stopping`. A message
    that finds `closing` still queues for the next session; one that finds
    `stopping` is dropped, as the stop drops the queue. Written only under the
    lock, so the engine pid is published in the same hold that spawned it."""
    atomic_write_text(
        _state_file(home),
        json.dumps({
            "pid": pid,
            "engine_pid": engine_pid,
            "engine_start": process_start_token(engine_pid) if engine_pid else None,
            "status": status,
        }),
    )


def _end_published_engine(state: dict) -> None:
    """Kill the engine a bridge published and did not end itself: the bridge
    died outright (Windows TerminateProcess, SIGKILL) before its cleanup ran."""
    engine_pid = state.get("engine_pid")
    if not (isinstance(engine_pid, int) and engine_pid > 0 and pid_alive(engine_pid)):
        return
    stamped = state.get("engine_start")
    if stamped and process_start_token(engine_pid) not in (None, stamped):
        return
    terminate_group(engine_pid)


def _sweep_group(pgid: int) -> None:
    """End what is left of an exited bridge's process group: the tools its
    engine started share that group and outlive both. A group with no
    members left is a no-op; a pid is never signaled on its own."""
    if os.name != "posix":
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except OSError:
            return
        time.sleep(0.2)


def _try_lock(home: Path, seconds: float):
    """The lock within `seconds`, or None when its holder is stuck. Release
    with `.__exit__(None, None, None)`."""
    deadline = time.monotonic() + seconds
    while True:
        held = file_lock(_lock_file(home), blocking=False)
        try:
            held.__enter__()
            return held
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)


def _compose_claude_argv(
    template: list[str],
    env: dict[str, str],
    mcp_log_path: Path | None = None,
) -> list[str]:
    argv = [a for a in template if a != "{prompt}"]
    if "-p" not in argv and "--print" not in argv:
        argv.append("-p")
    at = argv.index("--") + 1 if "--" in argv else 1
    server_env = dict(env)
    if mcp_log_path is not None:
        server_env["A8S_MCP_LOG"] = str(mcp_log_path)
    argv[at:at] = ["--mcp-config", _mcp_servers_json(server_env, _mcp_command())]
    for i, token in enumerate(argv):
        if token == "--allowedTools" and i + 1 < len(argv):
            if MCP_CLAUDE_TOOL not in argv[i + 1]:
                argv[i + 1] = f"{argv[i + 1]} {MCP_CLAUDE_TOOL}".strip()
            break
    stream_flags = [
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    p_idx = argv.index("-p") if "-p" in argv else len(argv)
    argv[p_idx:p_idx] = stream_flags
    return argv


def _compose_agy_argv(
    template: list[str],
    _env: dict[str, str],
    _mcp_log_path: Path | None = None,
) -> list[str]:
    """Turn an `agy ... --print {prompt}` template into a resident `stream-json`
    invocation. `--print` in agy consumes the next token as its prompt unless the
    empty prompt is attached with `=` (`--print=`). The headless turn's
    `--print-timeout` goes: the bridge's window is the session's one limit,
    and agy's own would end it before the wrap-up turn."""
    argv = [a for a in template if a != "{prompt}"]
    if "--print-timeout" in argv:
        idx = argv.index("--print-timeout")
        del argv[idx:idx + 2]
    stream_flags = [
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--print=",
    ]
    for flag in ("--print", "-p"):
        if flag in argv:
            idx = argv.index(flag)
            argv[idx:idx + 1] = stream_flags
            return argv
    argv.extend(stream_flags)
    return argv


def _extract_claude_assistant_text(frame: dict) -> str:
    msg = frame.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts)
    return ""


def _encode_claude_user_turn(text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
        },
        ensure_ascii=False,
    )


def _parse_claude_frame(frame: dict) -> ParsedFrame:
    ftype = frame.get("type")
    if ftype == "assistant":
        return ParsedFrame(assistant_text=_extract_claude_assistant_text(frame))
    if ftype == "result":
        res_text = frame.get("result")
        return ParsedFrame(
            result_text=res_text.strip() if isinstance(res_text, str) else "",
            turn_completed=True,
        )
    return ParsedFrame()


def _encode_agy_user_turn(text: str) -> str:
    return json.dumps(
        {
            "event": "user",
            "message": {"role": "user", "content": text},
        },
        ensure_ascii=False,
    )


def _parse_agy_frame(frame: dict) -> ParsedFrame:
    if frame.get("event") != "result":
        return ParsedFrame()
    res = frame.get("result")
    if not isinstance(res, dict):
        return ParsedFrame(turn_completed=True)
    resp = res.get("response")
    err = res.get("error")
    return ParsedFrame(
        assistant_text=resp.strip() if isinstance(resp, str) else "",
        error_text=err.strip() if isinstance(err, str) else "",
        turn_completed=True,
    )


CLAUDE_ADAPTER = ResidentAdapter(
    compose_argv=_compose_claude_argv,
    encode_user_turn=_encode_claude_user_turn,
    parse_frame=_parse_claude_frame,
    tell_step=MCP_TELL_STEP,
)

AGY_ADAPTER = ResidentAdapter(
    compose_argv=_compose_agy_argv,
    encode_user_turn=_encode_agy_user_turn,
    parse_frame=_parse_agy_frame,
    tell_step=CLI_TELL_STEP,
)

RESIDENT_ADAPTERS: dict[str, ResidentAdapter] = {
    "claude": CLAUDE_ADAPTER,
    "ollama-claude": CLAUDE_ADAPTER,
    "agy": AGY_ADAPTER,
}


def compose_resident_argv(
    template: list[str],
    env: dict[str, str],
    *,
    engine: str,
    mcp_log_path: Path | None = None,
) -> list[str]:
    """Turn a headless turn template into a resident `stream-json` invocation
    using the engine's resident adapter."""
    return RESIDENT_ADAPTERS[engine].compose_argv(template, env, mcp_log_path)


def resident_boot_prompt(
    dir_path: Path,
    message: str,
    *,
    agent: str | None,
    engine: str = "claude",
    resident_seconds: int = DEFAULT_RESIDENT_SECONDS,
    lessons_cap: int,
    lessons_cap_bytes: int,
    copies: list[Path] | None = None,
) -> str:
    """The cold-boot prompt for a resident session: the one-shot turn's
    startup, closed by the resident steps instead of the exit steps."""
    from engines.run import scaffold_prompt

    return scaffold_prompt(
        dir_path,
        message,
        agent=agent,
        tell_step=RESIDENT_ADAPTERS[engine].tell_step,
        resident_minutes=max(1, round(resident_seconds / 60)),
        lessons_cap=lessons_cap,
        lessons_cap_bytes=lessons_cap_bytes,
        copies=copies,
    )


def wrapup_prompt(dir_path: Path, resident_seconds: int) -> str:
    status = dir_path / "STATUS.md"
    lessons = dir_path / "LESSONS.md"
    minutes = max(1, round(resident_seconds / 60))
    return (
        f"[Resident session window ({minutes}m) reached — wrap up and exit]\n"
        "1. Stop any background tasks or monitors you started during this session.\n"
        f"2. Rewrite {status} with sections: Current State, Important Context, "
        "Next Steps, Decisions (with rationale).\n"
        f"3. Append any genuinely new durable insights from this session to "
        f"{lessons}. {LESSON_LINE_RULE} Never edit AGENTS.md.\n"
        "4. Reply briefly when done; the session will close immediately after."
    )


def _enqueue_message(home: Path, message: str) -> None:
    msg_id = new_ulid()
    payload = {"id": msg_id, "queued_at": time.time(), "message": message}
    atomic_write_text(
        _inbox_dir(home) / f"{msg_id}.json", json.dumps(payload, ensure_ascii=False)
    )


def _inbox_waiting(home: Path) -> bool:
    inbox = _inbox_dir(home)
    return inbox.is_dir() and any(inbox.glob("*.json"))


def _drain_inbox(home: Path) -> list[dict]:
    """Every queued message, oldest first. The enqueuer writes each file whole
    (temp file plus rename), so a read here never sees half a message."""
    inbox = _inbox_dir(home)
    if not inbox.is_dir():
        return []
    items: list[dict] = []
    for path in sorted(inbox.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        except ValueError:
            data = None
        path.unlink(missing_ok=True)
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            items.append(data)
    return items


def _open_log(home: Path):
    log_path = home / "resident.log"
    try:
        if log_path.stat().st_size > LOG_ROTATE_BYTES:
            os.replace(log_path, home / "resident.log.1")
    except OSError:
        pass
    return log_path.open("ab")


def enqueue_or_start(
    engine: str,
    message: str,
    *,
    dir_path: Path,
    template: list[str],
    model: str | None,
    effort: str | None,
    agent: str | None,
    timeout: int,
    scaffold: bool = True,
    lessons_cap: int = 200,
    lessons_cap_bytes: int = 35 * 1024,
    env: dict[str, str] | None = None,
    charge_hook=None,
    memory: str = "off",
    memory_home: str | None = None,
    memory_writer: str | None = None,
    memory_people: str | None = None,
    memory_rig_context: dict | None = None,
) -> int:
    """Queue `message` for the agent's resident bridge, starting the bridge
    when none is running. Returns 0 at once; the router is never held up.

    The spec is rewritten on every call, so the next session the bridge starts
    runs with the newest model, effort and caps. A message that will open a
    session — no bridge, or one that is wrapping up — is the one charged."""
    home = resident_home(dir_path, agent)
    spawn_env = dict(os.environ if env is None else env)
    spec = {
        "engine": engine,
        "dir_path": str(dir_path.resolve()),
        "template": template,
        "model": model,
        "effort": effort,
        "agent": agent,
        "timeout": timeout,
        "scaffold": scaffold,
        "lessons_cap": lessons_cap,
        "lessons_cap_bytes": lessons_cap_bytes,
        "memory": memory,
        "memory_home": memory_home,
        "memory_writer": memory_writer,
        "memory_people": memory_people,
        "memory_rig_context": memory_rig_context,
    }

    with file_lock(_lock_file(home)):
        # Queued messages live in the agent's own working tree; the directory
        # ignores itself so the agent's own commits never carry them.
        ignore = home.parent / ".gitignore"
        if not ignore.is_file():
            atomic_write_text(ignore, "*\n")
        atomic_write_text(_spec_file(home), json.dumps(spec), mode=0o600)
        live_pid = read_live_pid(home)
        state = read_state(home) or {}
        if live_pid is not None:
            if state.get("status") == "stopping":
                return 0
            if state.get("status") == "closing" and charge_hook is not None:
                charge_hook()
            _enqueue_message(home, message)
            return 0

        if charge_hook is not None:
            charge_hook()
        _enqueue_message(home, message)
        clean_env = {k: v for k, v in spawn_env.items() if not k.startswith("A8S_TURN_")}

        flags: dict = {}
        if os.name == "nt":
            flags["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            flags["start_new_session"] = True

        with _open_log(home) as log_fh:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "serve", str(home)],
                cwd=str(dir_path),
                env=clean_env,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                close_fds=True,
                **flags,
            )
        _write_pid_files(home, proc.pid)
        _write_state(home, pid=proc.pid, engine_pid=None, status="starting")
    return 0


def _log(line: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{stamp}] {line.rstrip()}", flush=True)


def _end_engine(proc: subprocess.Popen) -> None:
    """The engine and its tools share the bridge's process group on POSIX,
    so the bridge ends only the engine; `stop_resident` sweeps the group once
    the bridge has exited."""
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _run_session(
    home: Path, spec: dict, stop_requested: threading.Event, current: dict
) -> int:
    """One engine process from first message to wrap-up. Returns its exit code."""
    from engines.run import prepare_lessons_copies, resolve_argv0

    engine = spec["engine"]
    dir_path = Path(spec["dir_path"])
    template = list(spec["template"])
    agent = spec.get("agent")
    resident_seconds = int(spec.get("timeout") or DEFAULT_RESIDENT_SECONDS)
    scaffold = bool(spec.get("scaffold", True))
    lessons_cap = int(spec.get("lessons_cap") or 200)
    lessons_cap_bytes = int(spec.get("lessons_cap_bytes") or 35 * 1024)
    memory_mode = spec.get("memory") or "off"

    copies = (
        prepare_lessons_copies(
            dir_path, lessons_cap=lessons_cap, lessons_cap_bytes=lessons_cap_bytes
        )
        if scaffold
        else None
    )

    adapter = RESIDENT_ADAPTERS[engine]
    argv = resolve_argv0(
        compose_resident_argv(
            template, dict(os.environ), engine=engine,
            mcp_log_path=home / "mcp-calls.jsonl",
        )
    )
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("A8S_TURN_")}

    my_pid = os.getpid()
    # Spawn and publish in one hold of the lock: a stop that takes the lock
    # sees either no engine or its pid, never an engine it cannot name.
    with file_lock(_lock_file(home)):
        if stop_requested.is_set() or (read_state(home) or {}).get("status") == "stopping":
            stop_requested.set()
            return 0
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(dir_path),
                env=child_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            dropped = _drain_inbox(home)
            _log(f"resident failed to spawn {argv[0]!r}: {exc}; dropped {len(dropped)} message(s)")
            return 1
        current["proc"] = proc
        _write_state(home, pid=my_pid, engine_pid=proc.pid, status="running")
    started_at = time.time()

    def set_status(status: str) -> None:
        with file_lock(_lock_file(home)):
            if (read_state(home) or {}).get("status") != "stopping":
                _write_state(home, pid=my_pid, engine_pid=proc.pid, status=status)

    _log(
        f"resident session started (bridge pid={my_pid}, {engine} pid={proc.pid}, "
        f"window={resident_seconds}s)"
    )

    turns_completed = threading.Condition()
    in_flight_turns = 0
    collected_outputs: list[str] = []

    def _stdout_reader() -> None:
        nonlocal in_flight_turns
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                _log(line)
                continue
            if not isinstance(frame, dict):
                continue
            parsed = adapter.parse_frame(frame)
            if parsed.assistant_text:
                collected_outputs.append(parsed.assistant_text)
                _log(parsed.assistant_text)
            if parsed.error_text:
                _log(f"error: {parsed.error_text}")
            if parsed.turn_completed:
                if parsed.result_text and not collected_outputs:
                    collected_outputs.append(parsed.result_text)
                with turns_completed:
                    if in_flight_turns > 0:
                        in_flight_turns -= 1
                    turns_completed.notify_all()

    reader = threading.Thread(target=_stdout_reader, daemon=True)
    reader.start()

    def send_user_turn(prompt_text: str) -> bool:
        nonlocal in_flight_turns
        if proc.poll() is not None or proc.stdin is None:
            return False
        try:
            with turns_completed:
                in_flight_turns += 1
            proc.stdin.write(adapter.encode_user_turn(prompt_text) + "\n")
            proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def wait_for_quiet(timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        with turns_completed:
            while in_flight_turns > 0 and proc.poll() is None and not stop_requested.is_set():
                rem = deadline - time.monotonic()
                if rem <= 0:
                    return False
                turns_completed.wait(timeout=min(rem, 0.25))
            return in_flight_turns == 0

    booted = False
    memory_turn = None

    try:
        while not stop_requested.is_set() and proc.poll() is None:
            if time.time() - started_at >= resident_seconds:
                break

            batch = _drain_inbox(home)
            if batch:
                combined = "\n\n".join(item["message"] for item in batch)
                if not booted:
                    booted = True
                    if scaffold:
                        prompt_text = resident_boot_prompt(
                            dir_path,
                            combined,
                            agent=agent,
                            engine=engine,
                            resident_seconds=resident_seconds,
                            lessons_cap=lessons_cap,
                            lessons_cap_bytes=lessons_cap_bytes,
                            copies=copies,
                        )
                    else:
                        prompt_text = combined
                    if memory_mode != "off":
                        import engine_memory

                        try:
                            memory_turn = engine_memory.Turn(
                                engine=engine,
                                model=spec.get("model"),
                                effort=spec.get("effort"),
                                agent=agent,
                                directory=dir_path,
                                message=combined,
                                mode=memory_mode,
                                home=spec.get("memory_home"),
                                writer=spec.get("memory_writer"),
                                people=spec.get("memory_people"),
                                env=os.environ,
                                rig_context=spec.get("memory_rig_context"),
                            )
                            prompt_text = memory_turn.inject(prompt_text)
                        except Exception as exc:
                            engine_memory.note(f"memory unavailable: {exc}")
                            memory_turn = None
                else:
                    prompt_text = combined

                if not send_user_turn(prompt_text):
                    break

            time.sleep(POLL_SECONDS)

        if not stop_requested.is_set() and proc.poll() is None and booted:
            set_status("closing")
            wait_for_quiet(timeout_s=WRAPUP_WAIT_SECONDS)
            if proc.poll() is None and not stop_requested.is_set() and scaffold:
                _log(f"resident window ({resident_seconds}s) reached — sending wrap-up turn")
                if send_user_turn(wrapup_prompt(dir_path, resident_seconds)):
                    wait_for_quiet(timeout_s=WRAPUP_TURN_SECONDS)

        if stop_requested.is_set():
            _end_engine(proc)
        else:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _end_engine(proc)
    finally:
        if proc.poll() is None:
            _end_engine(proc)
        current["proc"] = None
        reader.join(timeout=2)
        exit_code = proc.returncode if proc.returncode is not None else 1
        if not booted:
            # The engine ended before it read anything. A failed wake simply
            # fails: drop what it never read rather than respawn on it forever.
            dropped = _drain_inbox(home)
            if dropped:
                _log(f"engine exited before its first turn; dropped {len(dropped)} message(s)")
        if memory_turn is not None:
            try:
                memory_turn.finish("\n\n".join(collected_outputs), exit_code)
            except Exception:
                pass
        _log(f"resident session exited (code {exit_code})")

    return exit_code


def serve_resident(home: Path) -> int:
    """Run the resident bridge for `home`: one session, then another while
    messages are waiting, then exit. The pid files are cleared under the same
    lock the enqueuer takes, so a message either reaches this bridge's next
    session or starts a new bridge — never neither."""
    stop_requested = threading.Event()
    current: dict = {"proc": None}

    # Installed before any engine exists. The handler only signals: the
    # session loop sees the flag within one poll and ends the engine itself.
    def _on_signal(signum, _frame):
        stop_requested.set()
        proc = current["proc"]
        if proc is not None and proc.poll() is None:
            proc.terminate()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    my_pid = os.getpid()
    exit_code = 0
    while True:
        try:
            spec = json.loads(_spec_file(home).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _log(f"resident spec unreadable: {exc}")
            with file_lock(_lock_file(home)):
                _drain_inbox(home)
                _clear_pid_files(home, expected_pid=my_pid)
            return 1
        exit_code = _run_session(home, spec, stop_requested, current)
        with file_lock(_lock_file(home)):
            if stop_requested.is_set() or (read_state(home) or {}).get("status") == "stopping":
                # A stop is a stop: what was queued must not replay as live
                # input when the node next starts.
                dropped = _drain_inbox(home)
                if dropped:
                    _log(f"stopped; dropped {len(dropped)} queued message(s)")
                _clear_pid_files(home, expected_pid=my_pid)
                return exit_code
            if not _inbox_waiting(home):
                _clear_pid_files(home, expected_pid=my_pid)
                return exit_code
            _write_state(home, pid=my_pid, engine_pid=None, status="starting")


def stop_resident(
    dir_path: Path,
    agent: str | None,
    *,
    grace_seconds: float = 10.0,
    lock_seconds: float = 5.0,
) -> bool:
    """End the agent's resident session now: no wrap-up turn, queued messages
    dropped. Returns False when no session was running.

    Ownership is decided under the lock the enqueuer and the bridge take: the
    session is marked `stopping`, so no message joins it and the bridge spawns
    no new engine. The bridge then gets SIGTERM and ends its engine itself;
    one still alive after `grace_seconds` is killed with its process group,
    which on POSIX holds the engine too. A published engine that outlived its
    bridge is killed last. Only the stopped generation's files are cleared: a
    bridge that started after this one exited keeps its queue and identity. A
    holder that keeps the lock past `lock_seconds` is treated as stuck and
    killed without waiting."""
    home = resident_home(dir_path, agent)
    held = _try_lock(home, lock_seconds)
    try:
        pid = read_live_pid(home)
        if pid is None:
            if held is not None:
                _end_published_engine(read_state(home) or {})
                _drain_inbox(home)
                _clear_pid_files(home)
            return False
        if held is not None:
            state = read_state(home) or {}
            _write_state(
                home, pid=pid, engine_pid=state.get("engine_pid"), status="stopping"
            )
    finally:
        if held is not None:
            held.__exit__(None, None, None)

    if held is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline and read_live_pid(home) == pid:
            time.sleep(0.1)
    if read_live_pid(home) == pid:
        terminate_group(pid)
    # The bridge leads its own group (spawned with start_new_session), so its
    # pid is the group id even after it exited.
    _sweep_group(pid)

    with file_lock(_lock_file(home)):
        current = read_live_pid(home)
        if current is not None and current != pid:
            return True
        _end_published_engine(read_state(home) or {})
        _drain_inbox(home)
        _clear_pid_files(home, expected_pid=pid)
    return True


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "serve":
        raise SystemExit(serve_resident(Path(sys.argv[2])))
    raise SystemExit(2)
