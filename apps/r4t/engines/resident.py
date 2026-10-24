"""Resident engine session — one long-running engine process per agent
(`stream-json` on `claude`, `ollama-claude` and `agy`; `codex app-server`
JSON-RPC on `codex`; `agent acp` JSON-RPC on `cursor`), fed incoming messages
over stdin and wrapped up after its timeout (default 45m) of quiet. The timeout is
a quiet window, not the session's life: a turn in flight never expires the
session, and any turn sent or frame received restarts the window.

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
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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

from engines.acp import AcpSession, ParsedFrame
from engines.run import (
    CLI_TELL_STEP, CODEX_MCP_TELL_STEP, LESSON_LINE_RULE, MCP_TELL_STEP, RunError,
)
from rig import (
    MCP_CLAUDE_TOOL, _mcp_codex_override, _mcp_command, _mcp_servers_json,
    _toml_string,
)

RESIDENT_ENGINES = frozenset({"agy", "claude", "codex", "cursor", "ollama-claude"})
RESIDENT_DIR_NAME = ".engine-resident"
# Quiet time, not session life: the session ends after this many seconds with
# no turn in flight and no activity.
DEFAULT_RESIDENT_SECONDS = 2700
# The bridge's inbox poll. Latency of a fraction of a second costs nothing
# against a turn; a tighter loop costs CPU for the whole window.
POLL_SECONDS = 0.5
LOG_ROTATE_BYTES = 1024 * 1024
# At the end of the quiet window: how long a turn still running may finish before
# the wrap-up turn is queued behind it, and how long the wrap-up itself gets.
WRAPUP_WAIT_SECONDS = 120
WRAPUP_TURN_SECONDS = 300

class EngineSession(Protocol):
    """Per-process state for an engine whose protocol is not one line per
    turn. Built once per engine process; `turn_lines` runs on the bridge's
    loop thread and `on_frame` on its reader thread."""

    def opening_lines(self) -> list[str]: ...
    def turn_lines(self, text: str) -> list[str]: ...
    def on_frame(self, frame: dict) -> ParsedFrame: ...


@dataclass(frozen=True)
class ResidentAdapter:
    compose_argv: Callable[[list[str], dict[str, str], Path | None], list[str]]
    encode_user_turn: Callable[[str], str]
    parse_frame: Callable[[dict], ParsedFrame]
    tell_step: str
    # None: the stateless path above. Set: called with the composed argv, the
    # session's directory, and the one env dict `_run_session` built, once per
    # engine process, and used in place of `encode_user_turn` and `parse_frame`.
    session: Callable[[list[str], Path, dict[str, str]], EngineSession] | None = None
    # False: the engine's stderr goes straight to resident.log instead of
    # sharing its stdout pipe, where a stderr line can split a long frame.
    merge_stderr: bool = True


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


_KEEP = object()


def _write_state(
    home: Path,
    *,
    pid: int,
    engine_pid: int | None,
    status: str,
    turns: int = 0,
    engine_start: str | None | object = _KEEP,
    engine_group: int | None | object = _KEEP,
) -> None:
    """`status` is `starting`, `running`, `closing` or `stopping`. A message
    that finds `closing` still queues for the next session; one that finds
    `stopping` is dropped, as the stop drops the queue. `turns` is the count
    of turns in flight in the session, for `status_resident`. Written only
    under the lock, so the engine pid is published in the same hold that
    spawned it.

    The start token and the process group are captured at spawn. A later
    write for the same pid keeps them; it does not read the token again
    after the leader has been reaped."""
    previous = read_state(home) or {}
    same = bool(engine_pid) and engine_pid == previous.get("engine_pid")
    if engine_start is _KEEP:
        if same and previous.get("engine_start"):
            start = previous.get("engine_start")
        else:
            start = process_start_token(engine_pid) if engine_pid else None
    else:
        start = engine_start
    if engine_group is _KEEP:
        group = previous.get("engine_group") if same else None
    else:
        group = engine_group
    payload: dict = {
        "pid": pid,
        "engine_pid": engine_pid,
        "engine_start": start,
        "status": status,
        "turns": turns,
    }
    if isinstance(group, int) and group > 0:
        payload["engine_group"] = group
    atomic_write_text(_state_file(home), json.dumps(payload))


def _published_token_matches(state: dict, pid: int) -> bool:
    """False when `pid` is alive and its start token is not the one published."""
    stamped = state.get("engine_start")
    if not stamped or not pid_alive(pid):
        return True
    return process_start_token(pid) in (None, stamped)


def _published_group_from_this_boot(state: dict) -> bool:
    """True when `engine_start` was captured on this kernel boot.

    Linux tokens lead with the boot id. A dead leader's group number can
    belong to an unrelated process after a reboot, so that group is signaled
    only when the ids match. With no boot id to read, the caller cannot
    apply this check."""
    token = state.get("engine_start")
    if not isinstance(token, str) or not token:
        return False
    try:
        current = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return False
    return bool(current) and token.startswith(current + " ")


def _boot_id_readable() -> bool:
    return Path("/proc/sys/kernel/random/boot_id").is_file()


def _has_start_token_reader() -> bool:
    """False on Windows, which has no start-token reader yet.

    The dead-bridge sweep in `enqueue_or_start` uses the token to tell the
    published engine from a pid the OS reused. Without a reader that sweep
    can end a live stranger, so enqueue skips it. `stop` still signals a
    recorded pid, as it did before the reader existed."""
    return os.name != "nt"


def _end_published_engine(state: dict) -> None:
    """Kill the engine a bridge published and did not end itself: the bridge
    died outright (Windows TerminateProcess, SIGKILL) before its cleanup ran.

    An ACP engine published its process group at spawn. That group is
    signaled after the leader has been reaped only when the published start
    token is from this boot. A live pid whose start token does not match
    the published one is left alone."""
    engine_pid = state.get("engine_pid")
    group = state.get("engine_group")
    leader = engine_pid if isinstance(engine_pid, int) and engine_pid > 0 else None
    if isinstance(group, int) and group > 0:
        leader_alive = leader is not None and pid_alive(leader)
        if leader_alive and not _published_token_matches(state, leader):
            return
        if (
            not leader_alive
            and _boot_id_readable()
            and not _published_group_from_this_boot(state)
        ):
            return
        if os.name != "posix":
            if leader is not None and pid_alive(leader):
                try:
                    os.kill(leader, signal.SIGTERM)
                except OSError:
                    pass
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(group, sig)
            except OSError:
                return
            time.sleep(0.2)
        return
    if leader is None or not pid_alive(leader):
        return
    if not _published_token_matches(state, leader):
        return
    terminate_group(leader)


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

# --- codex: `codex app-server`, newline-delimited JSON-RPC ------------------
#
# Proven against codex-cli 0.163.0 (wiki Engine-Codex). The app-server applies
# only `-c` keys and thread/start params: the top-level `-s`, `-a`, `-m` and
# `--dangerously-bypass-approvals-and-sandbox` parse and are then ignored, so
# the exec template is rewritten, never passed through. The thread/start answer
# carries the EFFECTIVE policy, and the session refuses to run on any other.

CODEX_APP_SERVER_PROVEN = "0.163.0"
CODEX_BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"
CODEX_APPROVAL_POLICY = "never"
# `(engine, permissions)` → the note a resident run prints in place of the
# one-shot one, whose flag the resident argv no longer carries.
RESIDENT_PERMISSION_NOTES = {
    ("codex", "bypass"): (
        'codex resident \'bypass\' is -c sandbox_mode="danger-full-access" with '
        '-c approval_policy="never": the session is no longer confined to its '
        "workspace"
    ),
    ("cursor", "bypass"): (
        "cursor resident bypass keeps --force on agent acp. Measured on "
        "2026.10.22-27dcf5f: a shell call is not asked. The restricted "
        "definition does not pass --force, so a tool call is asked and "
        "rejected once"
    ),
}
_CODEX_DROPPED = frozenset({"{prompt}", "--skip-git-repo-check"})
_CODEX_SANDBOX_TYPES = {
    "read-only": "readOnly",
    "workspace-write": "workspaceWrite",
    "danger-full-access": "dangerFullAccess",
}
_CODEX_INIT_ID = 1
_CODEX_THREAD_ID = 2
# Under `never` no approval is ever asked; one that comes anyway is answered
# with the narrowest grant, never more than the configured sandbox.
_CODEX_REFUSALS = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "execCommandApproval": {"decision": "denied"},
    "applyPatchApproval": {"decision": "denied"},
    "mcpServer/elicitation/request": {"action": "decline", "content": None},
    "item/tool/requestUserInput": {"answers": {}},
}
_CODEX_VERSION_RE = re.compile(r"^[^/\s]+/(\d+(?:\.\d+)+)")


def _compose_codex_argv(
    template: list[str],
    env: dict[str, str],
    mcp_log_path: Path | None = None,
) -> list[str]:
    """Turn a `codex exec ... {prompt}` template into `codex app-server` with
    the same intent as `-c` keys: model, sandbox (the bypass flag is
    `danger-full-access`), approvals `never`, and the a8s MCP server. The
    server's cwd is this process's: the bridge runs in the session's
    directory."""
    rest = list(template[1:])
    if "exec" not in rest:
        raise RunError(f"codex resident needs a `codex exec` template, got {template!r}")
    rest.remove("exec")
    config: list[str] = []
    model: str | None = None
    sandbox = "workspace-write"
    i = 0
    while i < len(rest):
        token = rest[i]
        if token in _CODEX_DROPPED:
            i += 1
        elif token == CODEX_BYPASS_FLAG:
            sandbox = "danger-full-access"
            i += 1
        elif token in ("-c", "--config", "-m", "--model", "-s", "--sandbox") and i + 1 < len(rest):
            value = rest[i + 1]
            if token in ("-c", "--config"):
                config += ["-c", value]
            elif token in ("-m", "--model"):
                model = value
            else:
                sandbox = value
            i += 2
        else:
            raise RunError(
                f"codex resident cannot carry {token!r} to app-server, which "
                "takes its policy from -c keys only"
            )
    if sandbox not in _CODEX_SANDBOX_TYPES:
        raise RunError(f"codex resident: unknown sandbox {sandbox!r}")
    server_env = dict(env)
    if mcp_log_path is not None:
        server_env["A8S_MCP_LOG"] = str(mcp_log_path)
    argv = [template[0], "app-server", *config]
    if model:
        argv += ["-c", f"model={_toml_string(model)}"]
    argv += [
        "-c", f"sandbox_mode={_toml_string(sandbox)}",
        "-c", f"approval_policy={_toml_string(CODEX_APPROVAL_POLICY)}",
        "-c", _mcp_codex_override(server_env, Path.cwd(), _mcp_command()),
    ]
    return argv


def codex_sandbox_intent(argv: list[str]) -> str:
    """The `sandbox_mode` a composed app-server argv asks for."""
    for token in argv:
        if token.startswith("sandbox_mode="):
            return token.split("=", 1)[1].strip().strip('"')
    return "workspace-write"


def codex_version(user_agent: str) -> str | None:
    """The CLI version from the initialize answer's `userAgent`
    (`<client>/<version> (<os>) ...`)."""
    m = _CODEX_VERSION_RE.match(user_agent or "")
    return m.group(1) if m else None


def codex_policy_problem(result: dict, sandbox: str) -> str | None:
    """Why a thread/start answer is not the policy asked for, or None."""
    approval = result.get("approvalPolicy")
    got = (result.get("sandbox") or {}).get("type")
    want = _CODEX_SANDBOX_TYPES.get(sandbox)
    problems = []
    if approval != CODEX_APPROVAL_POLICY:
        problems.append(f"approvalPolicy {approval!r}, not {CODEX_APPROVAL_POLICY!r}")
    if got != want:
        problems.append(f"sandbox {got!r}, not {want!r}")
    return "; ".join(problems) or None


def _rpc(msg: dict) -> str:
    return json.dumps({"jsonrpc": "2.0", **msg}, ensure_ascii=False)


def codex_opening(cwd: str, *, ephemeral: bool = False) -> list[str]:
    params: dict = {"cwd": cwd}
    if ephemeral:
        params["ephemeral"] = True
    return [
        _rpc({"id": _CODEX_INIT_ID, "method": "initialize",
              "params": {"clientInfo": {"name": "r4t", "version": "0"}}}),
        _rpc({"method": "initialized", "params": {}}),
        _rpc({"id": _CODEX_THREAD_ID, "method": "thread/start", "params": params}),
    ]


class CodexSession:
    """One thread on one app-server process. One turn is on the wire at a
    time: app-server merges a turn/start sent mid-turn into the running turn
    and answers it with ONE turn/completed, which would leave the bridge
    counting a turn that never ends. Turns that arrive early (before the
    thread exists) or mid-turn wait in `held`, oldest first."""

    def __init__(self, argv: list[str], dir_path: Path, env: dict[str, str] | None = None):
        del env  # the bridge passes one env dict; app-server reads its own argv
        self.cwd = str(dir_path)
        self.sandbox = codex_sandbox_intent(argv)
        self.thread_id: str | None = None
        self.busy = False
        self.held: deque[str] = deque()
        self.turn_requests: set[int] = set()
        self.next_id = _CODEX_THREAD_ID + 1
        self.version: str | None = None
        self.lock = threading.Lock()

    def opening_lines(self) -> list[str]:
        return codex_opening(self.cwd)

    def _turn_start(self, text: str) -> str:
        rid = self.next_id
        self.next_id += 1
        self.turn_requests.add(rid)
        self.busy = True
        return _rpc({"id": rid, "method": "turn/start", "params": {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": text}],
        }})

    def _release(self) -> tuple[str, ...]:
        if self.thread_id is not None and not self.busy and self.held:
            return (self._turn_start(self.held.popleft()),)
        return ()

    def turn_lines(self, text: str) -> list[str]:
        with self.lock:
            if self.thread_id is None or self.busy:
                self.held.append(text)
                return []
            return [self._turn_start(text)]

    def on_frame(self, frame: dict) -> ParsedFrame:
        with self.lock:
            return self._on_frame(frame)

    def _on_frame(self, frame: dict) -> ParsedFrame:
        method = frame.get("method")
        if method is not None and "id" in frame:
            answer = _CODEX_REFUSALS.get(method)
            reply = (
                {"id": frame["id"], "result": answer}
                if answer is not None
                else {"id": frame["id"], "error": {"code": -32601, "message": f"r4t does not serve {method}"}}
            )
            return ParsedFrame(
                error_text=f"codex asked {method}; refused under approval policy never",
                replies=(_rpc(reply),),
            )
        if method is None:
            return self._on_response(frame)
        params = frame.get("params") or {}
        if method == "item/completed":
            item = params.get("item") or {}
            if params.get("threadId") == self.thread_id and item.get("type") == "agentMessage":
                text = item.get("text")
                return ParsedFrame(assistant_text=text.strip() if isinstance(text, str) else "")
            return ParsedFrame()
        if method == "turn/completed":
            if params.get("threadId") != self.thread_id:
                return ParsedFrame()
            turn = params.get("turn") or {}
            self.busy = False
            finals = [
                it.get("text") for it in turn.get("items") or []
                if isinstance(it, dict) and it.get("type") == "agentMessage"
                and isinstance(it.get("text"), str)
            ]
            error = ""
            if turn.get("status") in ("failed", "interrupted"):
                error = ((turn.get("error") or {}).get("message") or f"turn {turn.get('status')}")
            return ParsedFrame(
                result_text=finals[-1].strip() if finals else "",
                error_text=error,
                turn_completed=True,
                replies=self._release(),
            )
        if method == "error" and not params.get("willRetry", False):
            return ParsedFrame(error_text=str((params.get("error") or {}).get("message") or "codex error"))
        if method == "configWarning":
            _log(f"codex warning: {params.get('summary')}")
        return ParsedFrame()

    def _on_response(self, frame: dict) -> ParsedFrame:
        rid = frame.get("id")
        error = frame.get("error")
        message = str((error or {}).get("message") or error) if error is not None else ""
        if rid == _CODEX_INIT_ID:
            if error is not None:
                return ParsedFrame(error_text=f"codex initialize failed: {message}", fatal=True)
            self.version = codex_version((frame.get("result") or {}).get("userAgent", ""))
            note = "" if self.version == CODEX_APP_SERVER_PROVEN else (
                f" (not the proven {CODEX_APP_SERVER_PROVEN})"
            )
            _log(f"codex app-server {self.version or 'version unknown'}{note}")
            return ParsedFrame()
        if rid == _CODEX_THREAD_ID:
            if error is not None:
                return ParsedFrame(error_text=f"codex thread/start failed: {message}", fatal=True)
            result = frame.get("result") or {}
            problem = codex_policy_problem(result, self.sandbox)
            if problem:
                return ParsedFrame(
                    error_text=f"codex thread/start reports {problem}; ending the session",
                    fatal=True,
                )
            self.thread_id = (result.get("thread") or {}).get("id")
            if not self.thread_id:
                return ParsedFrame(error_text="codex thread/start returned no thread id", fatal=True)
            return ParsedFrame(replies=self._release())
        if rid in self.turn_requests:
            self.turn_requests.discard(rid)
            if error is not None:
                self.busy = False
                return ParsedFrame(
                    error_text=f"codex turn/start failed: {message}",
                    turn_completed=True,
                    replies=self._release(),
                )
        return ParsedFrame()


def _codex_unused_encode(_text: str) -> str:
    raise RuntimeError("codex turns go through CodexSession.turn_lines")


def _codex_unused_parse(_frame: dict) -> ParsedFrame:
    raise RuntimeError("codex frames go through CodexSession.on_frame")


def codex_app_server_probe(argv: list[str], cwd: Path, *, timeout: float = 15.0) -> dict:
    """The handshake a check runs: initialize, then an ephemeral thread/start
    (no rollout file, no model call). `argv` gets `--strict-config`, so an
    unknown `-c` key fails at once. Returns `version`, the thread/start
    `result`, and `error` (with the server's last stderr line) on failure."""
    import tempfile

    errors = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            argv + ["--strict-config"], cwd=str(cwd), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        errors.close()
        return {"error": f"cannot run {argv[0]} app-server: {exc}"}
    answers: dict = {}

    def read() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and "method" not in msg and msg.get("id") in (1, 2):
                answers[msg["id"]] = msg
                if msg["id"] == 2 or "error" in msg:
                    return

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    out: dict = {}
    try:
        assert proc.stdin is not None
        for line in codex_opening(str(cwd), ephemeral=True):
            proc.stdin.write(line + "\n")
        proc.stdin.flush()
        reader.join(timeout=timeout)
    except OSError as exc:
        out["error"] = f"app-server pipe broke: {exc}"
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        errors.seek(0)
        lines = [ln.strip() for ln in re.sub(r"\x1b\[[0-9;]*m", "", errors.read()).splitlines() if ln.strip()]
        errors.close()
    init, thread = answers.get(1), answers.get(2)
    if init and "result" in init:
        out["version"] = codex_version(init["result"].get("userAgent", ""))
    for name, answer in (("initialize", init), ("thread/start", thread)):
        if answer is not None and "error" in answer:
            out.setdefault("error", f"{name}: {answer['error'].get('message')}")
    if thread and "result" in thread:
        out["result"] = thread["result"]
    elif "error" not in out:
        tail = f": {lines[-1]}" if lines else ""
        out["error"] = f"app-server did not answer thread/start in {timeout:.0f}s{tail}"
    return out


# --- cursor: `agent acp`, newline-delimited JSON-RPC -----------------------
#
# Proven against agent 2026.10.22-27dcf5f on WSL (no prompt sent). `agent acp`
# speaks ACP. The generic session lives in engines/acp.py. This adapter adds
# only the Cursor pieces: cursor_login, the model catalog pin, and the two
# cursor/* requests. `--model` is not spawned. The slug is carried on the
# session env and pinned with session/set_config_option, because a flag left
# currentValue on composer-2.5. The catalog value is
# `composer-2.5[fast=true]`; the bare slug is the option's name.

CURSOR_ACP_PROVEN = "2026.10.22-27dcf5f"
_CURSOR_DROPPED = frozenset({"{prompt}", "-p", "--print", "--trust", "--approve-mcps", "acp"})


def _bracket_model(spec: str) -> tuple[str, dict[str, str]] | None:
    """`grok-4.6[effort=high,fast=true]` → base plus the bracket pairs.
    A bare slug, or a bracket body that is not `key=value` pairs, is None."""
    if "[" not in spec or not spec.endswith("]"):
        return None
    base, body = spec[:-1].split("[", 1)
    if not base or not body:
        return None
    pairs: dict[str, str] = {}
    for part in body.split(","):
        if "=" not in part:
            return None
        key, value = part.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not value:
            return None
        pairs[key] = value
    return base, pairs


def cursor_catalog_resolve(
    requested: str, options: list,
) -> tuple[str | None, str | None]:
    """`(value, None)` or `(None, reason)` from one scan of the catalog.

    An exact value wins, then an option whose `name` is the slug. Two
    options with that name and different values are ambiguous. Then a
    unique catalog value whose base matches and whose brackets contain
    every requested pair (`grok-4.6[effort=high]` matches
    `grok-4.6[effort=high,fast=true]`). Two such values are ambiguous.
    Any other miss names the catalog values.
    """
    values: list[str] = []
    by_name: dict[str, str] = {}
    ambiguous: set[str] = set()
    bracketed: list[tuple[str | None, str, dict[str, str], str]] = []
    for opt in options:
        if not isinstance(opt, dict):
            continue
        value = opt.get("value")
        if not isinstance(value, str):
            continue
        values.append(value)
        name = opt.get("name")
        if isinstance(name, str):
            previous = by_name.get(name)
            if previous is not None and previous != value:
                ambiguous.add(name)
            by_name[name] = value
        parsed = _bracket_model(value)
        if parsed is not None:
            bracketed.append((
                name if isinstance(name, str) else None,
                parsed[0], parsed[1], value,
            ))
    if requested in values:
        return requested, None
    if requested in ambiguous:
        return None, f"ambiguous model name {requested!r}"
    named = by_name.get(requested)
    if named is not None:
        return named, None
    wanted = _bracket_model(requested)
    if wanted is not None:
        base, pairs = wanted
        matches = [
            value
            for name, value_base, have, value in bracketed
            if (name == base or value_base == base)
            and all(have.get(key) == item for key, item in pairs.items())
        ]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, f"ambiguous model name {requested!r}"
    shown = ", ".join(values) or "(none)"
    return None, (
        f"cursor catalog has no model {requested!r}; catalog values: {shown}"
    )


def cursor_catalog_model(requested: str, options: list) -> str | None:
    """The catalog `value` for a preset model, or None when it does not resolve."""
    value, _reason = cursor_catalog_resolve(requested, options)
    return value


def cursor_catalog_refusal(requested: str, options: list) -> str:
    """Why `requested` did not resolve. The same scan as `cursor_catalog_model`."""
    _value, reason = cursor_catalog_resolve(requested, options)
    if reason is not None:
        return reason
    return f"cursor catalog has no model {requested!r}"


def _cursor_model_select(config_options: object) -> dict | None:
    if not isinstance(config_options, list):
        return None
    for opt in config_options:
        if isinstance(opt, dict) and opt.get("id") == "model":
            return opt
    return None


def cursor_mcp_servers(env: dict[str, str]) -> list[dict]:
    """The a8s server in the ACP `mcpServers` shape: `env` is a list of
    `{name, value}`, which is what `session/new` accepted."""
    from rig import MCP_SERVER_NAME, _mcp_command, _mcp_server_env

    command = _mcp_command()
    pinned = _mcp_server_env(env)
    return [{
        "name": MCP_SERVER_NAME,
        "command": command[0],
        "args": command[1:],
        "env": [{"name": key, "value": value} for key, value in pinned.items()],
    }]


def _compose_cursor_argv(
    template: list[str],
    env: dict[str, str],
    mcp_log_path: Path | None = None,
) -> list[str]:
    """Turn `agent -p --trust --force --approve-mcps --model … {prompt}` into
    `agent acp`, keeping `--force` only when the template still has it
    (`--permissions bypass`). The restricted resident path strips `--force`
    before this runs. The model slug is written onto the env dict as
    `R4T_SESSION_MODEL` and stripped before the child is spawned; the session
    pins the catalog value itself. The MCP log path is written onto that same env dict.
    `agent acp` has no flag for it, and this function does not touch
    `os.environ`."""
    if not template or template[0] in ("-p", "--print"):
        raise RunError(f"cursor resident needs an `agent` template, got {template!r}")
    if mcp_log_path is not None:
        env["A8S_MCP_LOG"] = str(mcp_log_path)
    model: str | None = None
    force = False
    i = 0
    rest = list(template[1:])
    while i < len(rest):
        token = rest[i]
        if token in _CURSOR_DROPPED:
            i += 1
        elif token == "--force":
            force = True
            i += 1
        elif token == "--model" and i + 1 < len(rest):
            model = rest[i + 1]
            i += 2
        elif token.startswith("--model="):
            model = token.split("=", 1)[1]
            i += 1
        else:
            raise RunError(
                f"cursor resident cannot carry {token!r} to `agent acp`"
            )
    argv = [template[0]]
    if model:
        env["R4T_SESSION_MODEL"] = model
    if force:
        argv.append("--force")
    argv.append("acp")
    return argv


def cursor_permission_outcome(options: object, *, allow: bool) -> dict:
    """The ACP `session/request_permission` result. `allow` is `--force` on
    the composed argv, which the resident bypass path keeps."""
    from engines.acp import permission_outcome

    return permission_outcome(options, allow=allow)


class CursorSession(AcpSession):
    """Cursor's ACP session: auth, the model pin, and the two cursor requests.
    The handshake, the one-prompt rule, and permission-by-kind are generic.

    Measured on agent 2026.10.22-27dcf5f: stdin EOF does not end `agent acp`.
    The stop closes stdin, waits 1.5 s, signals the process group recorded
    at spawn with SIGTERM, waits until that group is empty or 2 s pass, then
    SIGKILL. On Windows the leader is terminate(), the grace, then kill().
    The next ACP engine has to be measured the same way; do not assume it
    exits on EOF."""

    def __init__(
        self, argv: list[str], dir_path: Path, env: dict[str, str] | None = None,
    ):
        super().__init__(argv, dir_path, env)
        self.model_requested = self.env.get("R4T_SESSION_MODEL") or None
        self.model_value: str | None = None
        self.model_pin_id: int | None = None
        if self.model_requested:
            self.settled = False

    def auth_method(self) -> str:
        return "cursor_login"

    def allows_permission(self) -> bool:
        return "--force" in self.argv

    def mcp_servers(self) -> list:
        return cursor_mcp_servers(self.env)

    def after_session_new(self, result: dict) -> ParsedFrame | None:
        if not self.model_requested:
            select = _cursor_model_select(result.get("configOptions"))
            if select and isinstance(select.get("currentValue"), str):
                self.model_value = select["currentValue"]
            self.settled = True
            return None
        select = _cursor_model_select(result.get("configOptions"))
        if select is None:
            return self._fail("cursor session/new returned no model catalog")
        resolved = cursor_catalog_model(
            self.model_requested, select.get("options") or [],
        )
        if resolved is None:
            return self._fail(cursor_catalog_refusal(
                self.model_requested, select.get("options") or [],
            ))
        self.model_value = resolved
        if select.get("currentValue") == resolved:
            self.settled = True
            return None
        self.model_pin_id = self.next_id
        self.next_id += 1
        return ParsedFrame(replies=(_rpc({
            "id": self.model_pin_id,
            "method": "session/set_config_option",
            "params": {
                "sessionId": self.session_id,
                "configId": "model",
                "value": resolved,
            },
        }),))

    def on_extra_response(self, frame: dict) -> ParsedFrame | None:
        if self.model_pin_id is None or frame.get("id") != self.model_pin_id:
            return None
        error = frame.get("error")
        if error is not None:
            message = str(error.get("message") or error) if isinstance(error, dict) else str(error)
            return self._fail(f"cursor model pin failed: {message}")
        select = _cursor_model_select((frame.get("result") or {}).get("configOptions"))
        current = select.get("currentValue") if select else None
        if current != self.model_value:
            return self._fail(
                f"cursor model pin left {current!r}, not {self.model_value!r}"
            )
        self.settled = True
        return self._maybe_ready()

    def on_vendor_request(self, frame: dict, method: str) -> ParsedFrame | None:
        if method == "cursor/ask_question":
            return ParsedFrame(
                error_text="cursor asked cursor/ask_question; skipped",
                replies=(_rpc({"id": frame["id"], "result": {"outcome": "skipped"}}),),
            )
        if method == "cursor/create_plan":
            return ParsedFrame(
                error_text="cursor asked cursor/create_plan; cancelled",
                replies=(_rpc({"id": frame["id"], "result": {"outcome": "cancelled"}}),),
            )
        return None


def _cursor_unused_encode(_text: str) -> str:
    raise RuntimeError("cursor turns go through CursorSession.turn_lines")


def _cursor_unused_parse(_frame: dict) -> ParsedFrame:
    raise RuntimeError("cursor frames go through CursorSession.on_frame")


def cursor_acp_probe(
    argv: list[str], cwd: Path, *, timeout: float = 20.0,
    env: dict[str, str] | None = None,
) -> dict:
    """The handshake a check runs: initialize, authenticate, session/new, and
    the model pin. No session/prompt, so no turn is spent. `env` is the dict
    compose filled; the child still inherits the real environment, without
    the session-only keys. Returns `model` (the catalog value in force) and
    `error`. A missing initialize or authenticate reply, or a protocol
    version other than 1, leaves the session unready and is an error."""
    import tempfile

    session_env = dict(os.environ)
    if env:
        session_env.update(env)
    session = CursorSession(argv, cwd, session_env)
    errors = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    try:
        leads_group = os.name != "nt"
        proc = subprocess.Popen(
            argv, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=errors, text=True, encoding="utf-8", errors="replace",
            start_new_session=leads_group,
        )
        engine_group = proc.pid if leads_group else None
    except OSError as exc:
        errors.close()
        return {"error": f"cannot run {argv[0]} acp: {exc}"}
    write_lock = threading.Lock()

    def write(line: str) -> None:
        assert proc.stdin is not None
        with write_lock:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()

    def read() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                session.error = "acp frame was not an object"
                return
            parsed = session.on_frame(msg)
            for reply in parsed.replies:
                try:
                    write(reply)
                except OSError:
                    return
            if parsed.fatal or session.ready:
                return

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    out: dict = {}
    try:
        for line in session.opening_lines():
            write(line)
        reader.join(timeout=timeout)
    except OSError as exc:
        out["error"] = f"agent acp pipe broke: {exc}"
    finally:
        stop_engine(
            proc, group=engine_group, grace_seconds=ACP_STOP_GRACE_SECONDS,
        )
        errors.seek(0)
        lines = [ln.strip() for ln in errors.read().splitlines() if ln.strip()]
        errors.close()
    if session.error:
        out["error"] = session.error
    elif not session.ready and "error" not in out:
        tail = f": {lines[-1]}" if lines else ""
        out["error"] = f"agent acp did not finish its handshake in {timeout:.0f}s{tail}"
    if session.model_value:
        out["model"] = session.model_value
    return out


CURSOR_ADAPTER = ResidentAdapter(
    compose_argv=_compose_cursor_argv,
    encode_user_turn=_cursor_unused_encode,
    parse_frame=_cursor_unused_parse,
    tell_step=MCP_TELL_STEP,
    session=CursorSession,
    merge_stderr=False,
)

CODEX_ADAPTER = ResidentAdapter(
    compose_argv=_compose_codex_argv,
    encode_user_turn=_codex_unused_encode,
    parse_frame=_codex_unused_parse,
    tell_step=CODEX_MCP_TELL_STEP,
    session=CodexSession,
    merge_stderr=False,
)

RESIDENT_ADAPTERS: dict[str, ResidentAdapter] = {
    "claude": CLAUDE_ADAPTER,
    "ollama-claude": CLAUDE_ADAPTER,
    "agy": AGY_ADAPTER,
    "codex": CODEX_ADAPTER,
    "cursor": CURSOR_ADAPTER,
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
        f"[Resident session quiet for {minutes}m — wrap up and exit]\n"
        "1. Stop any background tasks or monitors you started during this session.\n"
        f"2. Rewrite {status} with sections: Current State, Important Context, "
        "Next Steps, Decisions (with rationale).\n"
        f"3. Append any genuinely new durable insights from this session to "
        f"{lessons}. {LESSON_LINE_RULE} Never edit AGENTS.md.\n"
        "4. When done, say so in your reply text only; send no message. "
        "The session closes right after."
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
    session — no bridge, or one that is wrapping up — is the one charged.
    Where a start-token reader exists, a dead bridge's published engine is
    ended before the new bridge spawns. Where it does not, that sweep is
    skipped and only `stop` signals a recorded pid."""
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

        # A bridge that died outright left its engine in this record. End
        # that group before the new bridge's state replaces it, or a later
        # stop can never reach it. The same token and boot checks apply.
        # Without a start-token reader a live pid may be a reuse, so the
        # sweep is skipped there. stop still signals a recorded pid.
        if _has_start_token_reader():
            _end_published_engine(state)

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


# Every resident exit uses this sequence. An ACP engine is its own process
# group, so the last two signals hit the group. A process that shares the
# bridge's group is signaled by pid: killpg of that id would kill the bridge.
# The wait after stdin EOF is per engine, and only when the bridge ends the
# session itself. A stop request passes 0. A stop that arrives during the
# wait ends it, and the signal goes out at once. codex, claude and agy keep
# the 10 s they had before this session on that path; cursor ACP measured
# 1.5 s because agent acp ignores EOF.
ACP_CANCEL_WAIT_SECONDS = 1.5
EOF_GRACE_SECONDS = 10.0
ACP_STOP_GRACE_SECONDS = 1.5
ACP_STOP_TERM_SECONDS = 2.0


def _use_windows_stop() -> bool:
    """Windows has no SIGKILL and no killpg. Tests force this branch."""
    return os.name == "nt"


def _signal_group(pgid: int, sig: int) -> bool:
    """Signal the group recorded at spawn. True when that group is already empty."""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _signal_pid(proc: subprocess.Popen, sig: int) -> None:
    """Signal the leader only while it is still running. A reaped pid is not
    signaled: the recorded group, when there is one, is the target instead."""
    if proc.poll() is not None or not proc.pid:
        return
    try:
        os.kill(proc.pid, sig)
    except OSError:
        pass


def _group_empty(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _wait_group_empty(
    proc: subprocess.Popen, pgid: int, timeout: float,
) -> bool:
    """True once the recorded group has no members, or the wait runs out.

    poll() reaps a zombie leader. killpg(0) still sees that zombie, so
    without the reap every stop waits the full 2 s.
    """
    deadline = time.monotonic() + timeout
    while True:
        proc.poll()
        if _group_empty(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _wait_out_grace(
    proc: subprocess.Popen,
    grace: float,
    wake: threading.Event | None,
    also: threading.Event | None = None,
) -> None:
    """Wait out the EOF grace. `wake` or `also` ends it at once.

    A stop or a fatal that arrives while the engine is in this wait is the
    same as one that was already set: the caller signals immediately, with
    no grace left. The engine exiting on its own ends the wait too.
    """
    if grace <= 0:
        return
    deadline = time.monotonic() + grace
    while proc.poll() is None:
        if (wake is not None and wake.is_set()) or (
            also is not None and also.is_set()
        ):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))


def _stop_windows(
    proc: subprocess.Popen,
    grace: float,
    signaled: dict,
    wake: threading.Event | None = None,
    also: threading.Event | None = None,
) -> None:
    """Wait out the EOF grace, then terminate(), then up to 2 s, then kill().

    No SIGKILL and no killpg. An engine that exits during the grace is not
    terminated. A stop or a fatal that arrives during the grace ends the
    wait first.
    """
    _wait_out_grace(proc, grace, wake, also)
    if proc.poll() is None:
        signaled["hit"] = True
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=ACP_STOP_TERM_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    if proc.poll() is None:
        signaled["hit"] = True
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _stop_posix(
    proc: subprocess.Popen,
    group: int | None,
    grace: float,
    signaled: dict,
    wake: threading.Event | None = None,
    also: threading.Event | None = None,
) -> None:
    _wait_out_grace(proc, grace, wake, also)
    if group is not None:
        # The recorded group includes the leader while it lives. After the
        # reap, only that group is signaled; the pid itself is not. A leader
        # that already exited did so by itself: its code is not one of our signals.
        leader_alive = proc.poll() is None
        if not leader_alive and _group_empty(group):
            return
        if leader_alive:
            signaled["hit"] = True
        if _signal_group(group, signal.SIGTERM):
            return
        if _wait_group_empty(proc, group, ACP_STOP_TERM_SECONDS):
            return
        _signal_group(group, signal.SIGKILL)
    else:
        if proc.poll() is None:
            signaled["hit"] = True
            _signal_pid(proc, signal.SIGTERM)
        if proc.poll() is None:
            try:
                proc.wait(timeout=ACP_STOP_TERM_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            _signal_pid(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def stop_engine(
    proc: subprocess.Popen,
    *,
    group: int | None = None,
    cancel_line: str | None = None,
    write_line: Callable[[str], bool] | None = None,
    turn_settled: Callable[[], bool] | None = None,
    grace_seconds: float = EOF_GRACE_SECONDS,
    signaled: dict | None = None,
    wake: threading.Event | None = None,
    also: threading.Event | None = None,
) -> None:
    """End the engine and the children in the group recorded at spawn.

    session/cancel when a turn is in flight, then stdin EOF, then
    `grace_seconds`. A stop request passes 0. `wake` or `also` ends a grace
    that has already started, and the signal follows at once; cursor sends
    no cancel on that path when no turn is in flight. On POSIX the recorded group gets SIGTERM and is polled
    until it is empty or 2 s pass, then SIGKILL. `group` is that id
    (`proc.pid` when the engine was spawned with `start_new_session`); this
    function does not call getpgid. A leader that has already exited is not
    passed to os.kill. On Windows the leader waits out the grace, then
    terminate(), then up to 2 s, then kill().
    """
    if cancel_line and write_line is not None:
        try:
            write_line(cancel_line)
        except (BrokenPipeError, OSError, ValueError):
            pass
        if turn_settled is not None:
            deadline = time.monotonic() + ACP_CANCEL_WAIT_SECONDS
            while time.monotonic() < deadline and not turn_settled():
                time.sleep(0.05)
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except OSError:
            pass
    if signaled is None:
        signaled = {}
    if _use_windows_stop():
        _stop_windows(proc, grace_seconds, signaled, wake, also)
        return
    _stop_posix(proc, group, grace_seconds, signaled, wake, also)


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
    try:
        session_env = dict(os.environ)
        argv = resolve_argv0(
            compose_resident_argv(
                template, session_env, engine=engine,
                mcp_log_path=home / "mcp-calls.jsonl",
            )
        )
    except RunError as exc:
        dropped = _drain_inbox(home)
        _log(f"resident cannot compose {engine}: {exc}; dropped {len(dropped)} message(s)")
        return 1
    child_env = {
        k: v for k, v in session_env.items()
        if not k.startswith("A8S_TURN_") and not k.startswith("R4T_SESSION_")
    }

    my_pid = os.getpid()
    # Spawn and publish in one hold of the lock: a stop that takes the lock
    # sees either no engine or its pid, never an engine it cannot name.
    with file_lock(_lock_file(home)):
        if stop_requested.is_set() or (read_state(home) or {}).get("status") == "stopping":
            stop_requested.set()
            return 0
        leads_group = (
            os.name != "nt"
            and isinstance(adapter.session, type)
            and issubclass(adapter.session, AcpSession)
        )
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(dir_path),
                env=child_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # Unmerged, stderr is the bridge's own: resident.log.
                stderr=subprocess.STDOUT if adapter.merge_stderr else None,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                # ACP leads its own group so the stop can kill its children
                # without signaling the bridge. The group id is the pid, and
                # it has to be stored now: getpgid after the leader exits
                # cannot see the children it left behind.
                start_new_session=leads_group,
            )
            engine_group = proc.pid if leads_group else None
        except OSError as exc:
            dropped = _drain_inbox(home)
            _log(f"resident failed to spawn {argv[0]!r}: {exc}; dropped {len(dropped)} message(s)")
            return 1
        current["proc"] = proc
        _write_state(
            home, pid=my_pid, engine_pid=proc.pid, engine_group=engine_group,
            status="running",
        )
    last_activity = time.monotonic()
    session = (
        adapter.session(argv, dir_path, session_env) if adapter.session is not None else None
    )
    # The loop thread writes turns and the reader writes replies; one lock
    # keeps each line whole on the pipe.
    stdin_lock = threading.Lock()

    def write_lines(lines) -> bool:
        if not lines:
            return True
        if proc.poll() is not None or proc.stdin is None:
            return False
        try:
            with stdin_lock:
                for line in lines:
                    proc.stdin.write(line + "\n")
                proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def set_status(status: str) -> None:
        with file_lock(_lock_file(home)):
            current_state = read_state(home) or {}
            if current_state.get("status") != "stopping":
                _write_state(
                    home, pid=my_pid, engine_pid=proc.pid, status=status,
                    turns=current_state.get("turns", 0),
                )

    def publish_turns() -> None:
        # Same lock as every other state write, taken before the count is
        # read: two threads publishing cannot leave the older count last, and
        # the status read back under the lock is the one rewritten, so a
        # concurrent `closing` or `stopping` is never clobbered. Called
        # outside `turns_completed` so a stop holding the file lock cannot
        # stall the frame counters. Lock order is file lock, then condition.
        with file_lock(_lock_file(home)):
            current_state = read_state(home) or {}
            if current_state.get("status") in (None, "stopping"):
                return
            with turns_completed:
                count = in_flight_turns
            _write_state(
                home, pid=my_pid, engine_pid=proc.pid,
                status=current_state["status"], turns=count,
            )

    _log(
        f"resident session started (bridge pid={my_pid}, {engine} pid={proc.pid}, "
        f"quiet window={resident_seconds}s)"
    )

    turns_completed = threading.Condition()
    in_flight_turns = 0
    collected_outputs: list[str] = []
    fatal = threading.Event()

    end_flags = {"write_failed": False}

    def _stdout_reader() -> None:
        nonlocal in_flight_turns, last_activity
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
                # Claude and agy merge stderr onto this pipe, and a bare
                # JSON value there is not a frame. ACP and codex do not.
                if adapter.merge_stderr:
                    _log(line)
                    continue
                _log("resident engine sent a frame that was not an object; ending it")
                fatal.set()
                break
            # Under the condition: the running→closing decision below reads
            # this under the same lock, so a frame parsed before the decision
            # always counts and one parsed after it is after the window.
            with turns_completed:
                last_activity = time.monotonic()
            parsed = (
                session.on_frame(frame) if session is not None
                else adapter.parse_frame(frame)
            )
            if parsed.assistant_text:
                collected_outputs.append(parsed.assistant_text)
                _log(parsed.assistant_text)
            if parsed.error_text:
                _log(f"error: {parsed.error_text}")
            if parsed.turn_completed:
                if parsed.result_text and not collected_outputs:
                    collected_outputs.append(parsed.result_text)
                with turns_completed:
                    last_activity = time.monotonic()
                    if in_flight_turns > 0:
                        in_flight_turns -= 1
                    turns_completed.notify_all()
                publish_turns()
            if parsed.replies and not write_lines(parsed.replies):
                _log("resident engine closed stdin")
                end_flags["write_failed"] = True
                fatal.set()
                break
            if parsed.fatal:
                _log("resident engine cannot run this session; ending it")
                fatal.set()
                break

    reader = threading.Thread(target=_stdout_reader, daemon=True)
    reader.start()
    if session is not None and not write_lines(session.opening_lines()):
        _log("resident engine closed stdin before its handshake")
        end_flags["write_failed"] = True
        fatal.set()

    def send_user_turn(prompt_text: str) -> bool:
        nonlocal in_flight_turns, last_activity
        if proc.poll() is not None or proc.stdin is None:
            return False
        with turns_completed:
            in_flight_turns += 1
            last_activity = time.monotonic()
        publish_turns()
        lines = (
            session.turn_lines(prompt_text) if session is not None
            else [adapter.encode_user_turn(prompt_text)]
        )
        if write_lines(lines):
            return True
        _log("resident engine closed stdin during a turn")
        end_flags["write_failed"] = True
        fatal.set()
        return False

    def wait_for_quiet(timeout_s: float) -> bool:
        """True when no turn is in flight. A fatal frame or a stop ends the wait."""
        deadline = time.monotonic() + timeout_s
        with turns_completed:
            while (
                in_flight_turns > 0
                and proc.poll() is None
                and not stop_requested.is_set()
                and not fatal.is_set()
            ):
                rem = deadline - time.monotonic()
                if rem <= 0:
                    return False
                turns_completed.wait(timeout=min(rem, 0.25))
            return in_flight_turns == 0 and not fatal.is_set() and not stop_requested.is_set()

    def drop_held_turns(cause: str) -> None:
        """A handshake that never finished has nothing to wrap up. The turn
        is taken off the session and off the in-flight count, the same way a
        turn is not left held when the engine dies before it answers."""
        nonlocal in_flight_turns
        held_n = 0
        inflight = 0
        if session is not None and hasattr(session, "held"):
            lock = getattr(session, "lock", None)
            if lock is not None:
                with lock:
                    held_n = len(session.held)
                    session.held.clear()
            else:
                held_n = len(session.held)
                session.held.clear()
        with turns_completed:
            inflight = in_flight_turns
            if in_flight_turns:
                in_flight_turns = 0
                turns_completed.notify_all()
        publish_turns()
        dropped = max(held_n, inflight)
        if dropped:
            _log(f"dropped {dropped} held message(s): {cause}")

    booted = False
    memory_turn = None

    def end_engine() -> None:
        line = None
        if isinstance(session, AcpSession) and not fatal.is_set():
            line = session.cancel_line()
        # The EOF grace is only a quiet expiry or a wrap-up that finished.
        # A stop, a fatal, a missed deadline, a failed write, or an engine
        # exit signals at once.
        abrupt = (
            stop_requested.is_set()
            or fatal.is_set()
            or end_flags["write_failed"]
            or handshake_missed
            or wrapup_missed
            or not handshake_ready
            or proc.poll() is not None
        )
        if abrupt:
            grace = 0.0
        elif isinstance(session, AcpSession):
            grace = ACP_STOP_GRACE_SECONDS
        else:
            grace = EOF_GRACE_SECONDS

        def settled() -> bool:
            with turns_completed:
                return in_flight_turns == 0

        stop_engine(
            proc,
            group=engine_group,
            cancel_line=line,
            write_line=(lambda text: write_lines([text])) if line else None,
            turn_settled=settled if line else None,
            grace_seconds=grace,
            signaled=bridge_signaled,
            wake=stop_requested,
            also=fatal,
        )

    def quiet() -> bool:
        """No turn in flight and a whole window since the last activity.
        Caller holds `turns_completed`."""
        return in_flight_turns == 0 and time.monotonic() - last_activity >= resident_seconds

    handshake_missed = False
    handshake_ready = True
    wrapup_missed = False
    bridge_signaled: dict = {"hit": False}
    session_outcome = 1
    try:
        while not stop_requested.is_set() and proc.poll() is None:
            if fatal.is_set():
                break
            if session is not None:
                late = getattr(session, "deadline_error", lambda: None)()
                if late:
                    _log(late)
                    if not getattr(session, "handshake_done", True):
                        handshake_missed = True
                    break
            with turns_completed:
                idle = quiet()
            if idle and not _inbox_waiting(home):
                # The inbox check did I/O; decide again under the lock so a
                # frame that arrived meanwhile keeps the session running.
                with turns_completed:
                    if quiet():
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

            slept = 0.0
            while (
                slept < POLL_SECONDS
                and not fatal.is_set()
                and not stop_requested.is_set()
                and proc.poll() is None
            ):
                time.sleep(0.05)
                slept += 0.05

        handshake_ready = session is None or getattr(session, "handshake_done", True)
        crashed = proc.poll() is not None and proc.returncode not in (0, None)
        failed = (
            fatal.is_set()
            or handshake_missed
            or not handshake_ready
            or (crashed and not stop_requested.is_set())
        )
        if (
            not failed
            and not stop_requested.is_set()
            and proc.poll() is None
            and booted
        ):
            set_status("closing")
            if (
                wait_for_quiet(timeout_s=WRAPUP_WAIT_SECONDS)
                and proc.poll() is None
                and not stop_requested.is_set()
                and not fatal.is_set()
                and scaffold
            ):
                _log(f"resident quiet for {resident_seconds}s — sending wrap-up turn")
                if send_user_turn(wrapup_prompt(dir_path, resident_seconds)):
                    if not wait_for_quiet(timeout_s=WRAPUP_TURN_SECONDS):
                        # A fatal, a stop, or the engine exiting also ends
                        # the wait. The deadline is only the clock running out.
                        if (
                            proc.poll() is None
                            and not stop_requested.is_set()
                            and not fatal.is_set()
                        ):
                            wrapup_missed = True

        end_engine()
    finally:
        if proc.poll() is None:
            end_engine()
        current["proc"] = None
        reader.join(timeout=2)
        # The outcome is the session's, after the engine has exited and the
        # reader has stopped. A signal this bridge sent is not the engine's
        # own non-zero exit. A turn still open, a fatal frame, or a missed
        # deadline is not a clean session.
        with turns_completed:
            still_open = in_flight_turns > 0
        code = proc.returncode
        # Non-zero includes a negative code. An outside SIGKILL after EOF is
        # the engine's own failure. A signal this bridge sent does not count.
        self_nonzero = (
            code is not None and code != 0 and not bridge_signaled["hit"]
        )
        bad = (
            fatal.is_set()
            or still_open
            or self_nonzero
            or handshake_missed
            or wrapup_missed
            or not handshake_ready
        )
        session_outcome = 1 if bad else 0
        if bad:
            if stop_requested.is_set() and still_open:
                cause = "stop"
            elif handshake_missed or wrapup_missed:
                cause = "deadline"
            elif end_flags["write_failed"]:
                cause = "failed write"
            elif fatal.is_set():
                cause = "fatal"
            else:
                cause = "engine exit"
            drop_held_turns(cause)
        exit_code = proc.returncode if proc.returncode is not None else 1
        if not booted:
            # The engine ended before it read anything. A failed wake simply
            # fails: drop what it never read rather than respawn on it forever.
            dropped = _drain_inbox(home)
            if dropped:
                _log(f"engine exited before its first turn; dropped {len(dropped)} message(s)")
        if memory_turn is not None:
            try:
                memory_turn.finish("\n\n".join(collected_outputs), session_outcome)
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


def status_resident(dir_path: Path, agent: str | None) -> str | None:
    """`busy`, `resident`, or None when no session is up. None: no live
    bridge, or a bridge whose state says `stopping`. `busy`: a turn is in
    flight (`turns` > 0; a state written before the field existed reads as
    0), or the bridge is `starting` with a message queued. Everything else is
    `resident`, including `closing`. Reads only; takes no lock."""
    home = resident_home(dir_path, agent)
    if read_live_pid(home) is None:
        return None
    state = read_state(home) or {}
    status = state.get("status")
    if status == "stopping":
        return None
    turns = state.get("turns", 0)
    if isinstance(turns, int) and turns > 0:
        return "busy"
    if status == "starting" and _inbox_waiting(home):
        return "busy"
    return "resident"


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
    one still alive after `grace_seconds` is killed with its process group.
    The engine group published at spawn is signaled as well when the leader
    has already been reaped, but only when its start token is from this boot.
    A live pid whose start token does not match the published one is not
    signaled. Only the stopped generation's
    files are cleared: a
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
